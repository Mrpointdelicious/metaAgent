"""
创建日期：2026-10-25
文件功能：按统一请求响应契约调用多个独立知识服务，不维护厂商检索逻辑。
"""

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import httpx
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, model_validator

Domain = Literal["health", "product", "help"]


class NativeMaxKBConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: SecretStr = SecretStr("")
    password: SecretStr = SecretStr("")
    username_env: str = Field(default="", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$|^$")
    password_env: str = Field(default="", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$|^$")
    workspace_id: str = Field(default="default", pattern=r"^[A-Za-z0-9_-]{1,100}$")
    catalog_path: str = Field(min_length=1)
    catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    allowed_space_ids: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_native(self):
        for field in ("username", "password"):
            if getattr(self, field + "_env") and getattr(self, field).get_secret_value():
                raise ValueError("configure a native credential or its environment name, not both")
        if any(not space.strip() or space != space.strip() for space in self.allowed_space_ids):
            raise ValueError("native MaxKB space IDs must not be blank or padded")
        return self

    def credentials(self):
        return tuple(
            os.environ.get(getattr(self, field + "_env"), "")
            if getattr(self, field + "_env")
            else getattr(self, field).get_secret_value()
            for field in ("username", "password")
        )


class KnowledgeSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    description: str = Field(min_length=1, max_length=300)
    url: HttpUrl
    domains: list[Domain] = Field(default_factory=lambda: ["health"], min_length=1, max_length=3)
    # Application context supplies space only for these domains.
    scoped_domains: list[Domain] = Field(default_factory=list, max_length=3)
    token: SecretStr = SecretStr("")
    token_env: str = Field(default="", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$|^$")
    timeout_seconds: float = Field(default=20, gt=0, le=40)
    protocol: Literal["knowledge-search-v1", "maxkb"] = "knowledge-search-v1"
    maxkb: NativeMaxKBConfig | None = None

    @model_validator(mode="after")
    def validate_source(self):
        if self.url.username or self.url.password or self.url.fragment:
            raise ValueError("knowledge URLs must not contain credentials or fragments")
        if len(set(self.domains)) != len(self.domains):
            raise ValueError("knowledge source domains must be unique")
        if not set(self.scoped_domains) <= set(self.domains):
            raise ValueError("scoped_domains must belong to source domains")
        if self.token_env and self.token.get_secret_value():
            raise ValueError("configure token or token_env, not both")
        if self.protocol == "maxkb":
            if self.maxkb is None or set(self.scoped_domains) != set(self.domains):
                raise ValueError("native MaxKB requires configuration and scoped domains")
        elif self.maxkb is not None:
            raise ValueError("maxkb configuration requires the maxkb protocol")
        return self

    def credential(self):
        return (
            os.environ.get(self.token_env, "") if self.token_env else self.token.get_secret_value()
        )


@lru_cache(maxsize=1)
def contract():
    return json.loads(
        Path(__file__).with_name("knowledge-search-v1.openapi.json").read_text(encoding="utf-8")
    )


def validate_contract(name, payload):
    schema = {"$ref": f"#/components/schemas/{name}", "components": contract()["components"]}
    Draft202012Validator(schema).validate(payload)


def tool_schema(sources):
    """Project only model-owned request fields; identity and limits stay hidden."""
    properties = contract()["components"]["schemas"]["KnowledgeSearchRequest"]["properties"]
    domains = list(dict.fromkeys(domain for source in sources for domain in source.domains))
    return {
        "type": "object",
        "properties": {
            "query": properties["query"],
            "domain": {"type": "string", "enum": domains, "default": domains[0]},
            "source": {
                "type": "string",
                "enum": [source.id for source in sources],
                "description": "逻辑知识源；省略时使用配置中首个支持所选领域的知识源。",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }


class KnowledgeContractError(ValueError):
    pass


class KnowledgeAccessDenied(KnowledgeContractError):
    pass


async def search(source, payload, *, request_id, max_bytes):
    """Validate, send, and receive. No retries or fallback to a different knowledge source."""
    validate_contract("KnowledgeSearchRequest", payload)
    if source.protocol == "maxkb":
        from meta_agent.integrations.maxkb_native import search as native_search

        return await native_search(source, payload, request_id=request_id, max_bytes=max_bytes)
    token = source.credential()
    if not token:
        raise KnowledgeContractError("knowledge_credential_missing")
    async with (
        httpx.AsyncClient(
            timeout=source.timeout_seconds, follow_redirects=False, trust_env=False
        ) as client,
        client.stream(
            "POST",
            str(source.url),
            headers={"Authorization": "Bearer " + token, "X-Request-ID": request_id},
            json=payload,
        ) as response,
    ):
        response.raise_for_status()
        raw = bytearray()
        async for part in response.aiter_bytes():
            raw.extend(part)
            if len(raw) > max_bytes:
                raise KnowledgeContractError("oversized_knowledge_response")
    result = json.loads(raw)
    validate_contract("KnowledgeSearchResponse", result)
    ids = [row["id"] for row in result["sources"]]
    if len(set(ids)) != len(ids):
        raise KnowledgeContractError("duplicate_knowledge_source_id")
    # Keep the versioned projection; additive provider metadata stays out of model context.
    return {
        **{key: result[key] for key in ("status", "context", "gaps", "trace_id")},
        "sources": [
            {key: row[key] for key in ("id", "title", "locator", "ref")}
            for row in result["sources"]
        ],
    }
