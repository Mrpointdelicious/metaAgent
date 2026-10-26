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


async def search(source, payload, *, request_id, max_bytes):
    """Validate, send, and receive. No retries or fallback to a different knowledge source."""
    validate_contract("KnowledgeSearchRequest", payload)
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
