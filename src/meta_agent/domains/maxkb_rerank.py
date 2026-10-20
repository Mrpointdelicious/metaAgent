"""
创建日期：2026-10-19
文件功能：通过 MaxKB 正式应用 API 调用原生重排工作流并校验候选身份。
"""

import asyncio
import hashlib
import json
import math
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from meta_agent.domains.maxkb_client import json_request, validate_url
from meta_agent.domains.maxkb_contract import canonical


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    api_base: str = ""
    api_key: SecretStr = SecretStr("")
    application_id: UUID | None = None
    timeout_seconds: float = Field(default=8, gt=0, le=30)


class NativeWorkflowReranker:
    def __init__(self, config, admin_base):
        self.config = WorkflowConfig.model_validate(config or {})
        if self.config.enabled:
            base = validate_url(self.config.api_base, path="/chat/api")
            expected, actual = urlsplit(admin_base), urlsplit(base)
            key = self.config.api_key.get_secret_value()
            if (
                (expected.scheme, expected.hostname, expected.port)
                != (actual.scheme, actual.hostname, actual.port)
                or not self.config.application_id
                or not key.startswith(("agent-", "application-"))
                or len(key) > 256
                or any(c.isspace() for c in key)
            ):
                raise ValueError("Invalid native MaxKB rerank application configuration")

    @property
    def enabled(self):
        return self.config.enabled

    @staticmethod
    def candidates(hits, query, excerpt):
        selected, seen = [], set()
        for hit in hits:
            title = (hit["meta"]["title"] or "")[:200]
            content, _ = excerpt(hit["meta"]["text"], query)
            identity = title + content  # Native provider restores metadata by exact text.
            if identity in seen:
                continue
            seen.add(identity)
            candidate = {
                "id": hit["id"],
                "document_id": hit["meta"]["document_id"],
                "knowledge_id": hit["meta"]["knowledge_id"],
                "title": title,
                "content": content,
                "source_text_sha256": hashlib.sha256(hit["meta"]["text"].encode()).hexdigest(),
            }
            if len(canonical([c for _, c in selected] + [candidate])) > 250_000:
                break
            selected.append((hit, candidate))
            if len(selected) == 48:
                break
        return selected

    async def rank(self, http, query, candidates, request_id):
        if not candidates:
            return []
        if not self.enabled:
            raise HTTPException(503, "maxkb_rerank_not_configured")
        headers = {"Authorization": "Bearer " + self.config.api_key.get_secret_value()}
        base = self.config.api_base.rstrip("/")

        async def call(method, path, **kwargs):
            status, body = await json_request(
                http, method, base + path, headers=headers, limit=524288, **kwargs
            )
            if status != 200 or body.get("code") not in (0, 200):
                raise HTTPException(503, "maxkb_rerank_workflow_failed")
            return body.get("data")

        async with asyncio.timeout(self.config.timeout_seconds):
            # Every request gets a new session: no candidate history crosses a space boundary.
            chat_id = await call("GET", "/open")
            try:
                UUID(chat_id)
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(502, "maxkb_rerank_invalid_session") from None
            data = await call(
                "POST",
                "/chat_message/" + chat_id,
                json={
                    "message": query,
                    "stream": False,
                    "re_chat": False,
                    "form_data": {
                        "request_id": str(request_id),
                        "candidates": [c for _, c in candidates],
                    },
                },
            )
        try:
            body = json.loads(data["content"])
            rows = body["results"]
            if (
                body["schema_version"] != "maxkb-rerank-v1"
                or body["request_id"] != str(request_id)
                or not isinstance(rows, list)
                or len(rows) > min(6, len(candidates))
            ):
                raise ValueError("Invalid rerank envelope")
            submitted = {c["id"]: (hit, c) for hit, c in candidates}
            seen, ranked = set(), []
            for row in rows:
                meta = row["metadata"]
                hit, candidate = submitted[meta["id"]]
                score = meta["relevance_score"]
                if (
                    meta["id"] in seen
                    or any(meta.get(k) != value for k, value in candidate.items())
                    or type(score) not in (int, float)
                    or not math.isfinite(score)
                ):
                    raise ValueError("Invalid rerank candidate")
                seen.add(meta["id"])
                ranked.append({**hit, "score": float(score)})
            return sorted(ranked, key=lambda h: (-h["score"], h["id"]))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(502, "maxkb_rerank_response_invalid") from None
