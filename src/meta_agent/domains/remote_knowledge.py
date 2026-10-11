"""
创建日期：2026-10-11
文件功能：通过受限HTTP端口校验并映射隔离演示RAG证据，不改变Agent路由。
"""

import asyncio
import hashlib
import hmac
import json
import time
from typing import Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from pydantic import Field, ValidationError, model_validator

from meta_agent.contracts import DomainError, StrictModel, TaskResult
from meta_agent.domains.facts import FactBuilder
from meta_agent.infrastructure.limiter import PriorityLimiter

HTTPClient = httpx.AsyncClient


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


class RemoteSnippet(StrictModel):
    source_id: UUID
    chunk_id: UUID
    doc_ref: str = Field(pattern=r"^[a-z0-9-]+@[a-z0-9-]+#[a-z0-9-]+$", max_length=200)
    document_version: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    source: str = Field(pattern=r"^repo://demo/[a-z0-9-]+$", max_length=200)
    section: str = Field(min_length=1, max_length=200)
    section_id: str = Field(min_length=1, max_length=80)
    page: None
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=1600)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    score: float = Field(gt=0)

    @model_validator(mode="after")
    def source_consistency(self):
        code = self.source.removeprefix("repo://demo/")
        if (
            self.doc_ref != f"{code}@{self.document_version}#{self.section_id}"
            or self.source_id != uuid5(NAMESPACE_URL, self.source)
            or self.chunk_id
            != uuid5(NAMESPACE_URL, f"{self.source}@{self.document_version}#{self.section_id}")
            or self.end - self.start != len(self.text)
            or self.text_sha256 != hashlib.sha256(self.text.encode()).hexdigest()
        ):
            raise ValueError("Inconsistent source metadata")
        return self


class RemoteResponse(StrictModel):
    schema_version: Literal["demo-1"]
    request_id: UUID
    retrieval_run_id: UUID
    backend: Literal["portfolio_demo_bm25"]
    strategy: Literal["bm25-k1-1.2-b-0.75"]
    approval: Literal["technical_demo_only"]
    clinical_approval: Literal[False]
    corpus_version: str = Field(max_length=100)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["ok", "no_evidence"]
    elapsed_ms: float = Field(ge=0)
    budget_truncated: bool
    evidence: list[RemoteSnippet] = Field(max_length=6)
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


async def retrieve(settings, args, scope, request_id: str) -> RemoteResponse:
    url = urlsplit(settings.knowledge_rag_url)
    token = settings.knowledge_rag_token.get_secret_value()
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path != "/v1/experimental/demo/retrieve"
        or len(token) < 32
        or len(settings.knowledge_rag_corpus_sha256) != 64
    ):
        raise DomainError(
            "rag_configuration_invalid", "远程知识配置不完整。", outcome="unavailable"
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": request_id,
        "X-Tenant-ID": scope.tenant_id,
        "X-Actor-Role": scope.role,
        "X-Patient-Context": "false",
    }
    payload = {
        "query": args["query"],
        "domain": args["domain"],
        "scope_key": settings.knowledge_rag_scope_key,
        "corpus_version": settings.knowledge_rag_corpus_version,
        "top_k": settings.knowledge_rag_top_k,
        "max_chars": settings.knowledge_rag_max_chars,
    }
    try:
        async with (
            HTTPClient(
                timeout=settings.knowledge_rag_timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client,
            client.stream(
                "POST", settings.knowledge_rag_url, headers=headers, json=payload
            ) as response,
        ):
            if response.status_code != 200:
                code = {
                    401: "rag_unauthorized",
                    403: "rag_scope_denied",
                    404: "rag_unavailable",
                    409: "rag_version_unavailable",
                    422: "rag_request_invalid",
                    503: "rag_unavailable",
                }.get(response.status_code, "rag_service_failed")
                raise DomainError(
                    code,
                    "远程检索未成功，不能据此生成知识结论。",
                    outcome="unavailable",
                    retryable=response.status_code >= 500,
                )
            raw = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                raw.extend(chunk)
                if len(raw) > 65536:
                    raise ValueError("Oversized retrieval response")
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("Invalid envelope")
        signature = body.get("signature", "")
        expected = hmac.new(
            token.encode(),
            canonical({k: v for k, v in body.items() if k != "signature"}),
            hashlib.sha256,
        ).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid retrieval signature")
        result = RemoteResponse.model_validate(body)
        if (
            str(result.request_id) != request_id
            or result.corpus_version != settings.knowledge_rag_corpus_version
            or result.corpus_sha256 != settings.knowledge_rag_corpus_sha256
            or (result.status == "ok") != bool(result.evidence)
            or len(result.evidence) > settings.knowledge_rag_top_k
            or sum(len(s.text) for s in result.evidence) > settings.knowledge_rag_max_chars
            or len({s.doc_ref for s in result.evidence}) != len(result.evidence)
        ):
            raise ValueError("Retrieval envelope mismatch")
        return result
    except httpx.TimeoutException as exc:
        raise DomainError(
            "rag_timeout", "远程知识检索超时。", outcome="unavailable", retryable=True
        ) from exc
    except httpx.RequestError as exc:
        raise DomainError(
            "rag_connection_failed", "远程知识服务连接失败。", outcome="unavailable", retryable=True
        ) from exc
    except (ValueError, ValidationError, TypeError) as exc:
        raise DomainError(
            "rag_response_invalid", "远程证据或来源校验失败，已拒绝使用。", outcome="failed"
        ) from exc


class RemoteKnowledgeAdapter:
    async def execute(self, task, args, ctx):
        started = time.monotonic()
        trace = {"backend": "remote_rag", "request_id": str(uuid4()), "status": "started"}
        ctx.record.metrics.setdefault("knowledge_retrievals", []).append(trace)
        try:
            if (
                ctx.settings.app_env == "production"
                or ctx.scope.tenant_id != ctx.settings.knowledge_rag_tenant_id
                or ctx.scope.role != "operator"
                or any(
                    (
                        ctx.scope.patient_id,
                        ctx.scope.project_patient_id,
                        ctx.scope.iretour_patient_id,
                    )
                )
            ):
                raise DomainError(
                    "rag_demo_scope_denied",
                    "该演示知识库不允许患者或生产会话访问。",
                    outcome="unsupported",
                )
            if ctx.tool_calls >= ctx.settings.max_tool_calls:
                raise DomainError("tool_budget", "本轮工具调用预算已用完。", outcome="unavailable")
            ctx.tool_calls += 1
            if ctx.runtime_guard:
                await ctx.runtime_guard()
            # Both HTTP timeout and enclosing total deadline apply, including queue wait.
            lease = (
                ctx.tool_limiter.hold(1)
                if isinstance(ctx.tool_limiter, PriorityLimiter)
                else ctx.tool_limiter
            )
            async with (
                asyncio.timeout(min(ctx.remaining, ctx.settings.knowledge_rag_timeout_seconds)),
                lease,
            ):
                result = await retrieve(ctx.settings, args, ctx.scope, trace["request_id"])
            trace.update(
                status=result.status,
                retrieval_run_id=str(result.retrieval_run_id),
                corpus_version=result.corpus_version,
                strategy=result.strategy,
                hit_count=len(result.evidence),
                budget_truncated=result.budget_truncated,
            )
            if result.status == "no_evidence":
                return TaskResult(
                    task_id=task.task_id,
                    status="unavailable",
                    code="no_knowledge_evidence",
                    message="未检索到演示资料证据，不能编造来源或结论。",
                    outputs=dict(trace),
                )
            snippets = [s.model_dump(mode="json") for s in result.evidence]
            evidence = await ctx.repository.save_evidence(
                ctx.scope.scope_hash,
                "knowledge.search.remote_rag",
                ctx.record.request_id,
                {
                    "snippets": snippets,
                    "retrieval": result.model_dump(mode="json", exclude={"evidence", "signature"}),
                },
                result.corpus_version,
            )
            builder = FactBuilder(evidence)
            for i, snippet in enumerate(snippets):
                builder.record_ref = snippet["doc_ref"]
                builder.add(
                    f"/snippets/{i}/text",
                    "knowledge_excerpt",
                    f"{snippet['title']} · {snippet['section']} [{snippet['doc_ref']}]",
                    required=True,
                    uses=["display", "knowledge_explanation"],
                )
            for view in builder.views:
                view.fact.source_type = "document"
            return TaskResult(
                task_id=task.task_id,
                evidence_ids=[evidence.evidence_id],
                facts=builder.views,
                outputs={
                    **trace,
                    "doc_refs": [s["doc_ref"] for s in snippets],
                    "citations": [
                        {k: v for k, v in s.items() if k not in {"text", "score"}} for s in snippets
                    ],
                    "boundary": (
                        "合成方案；仅技术演示；不是临床证据、患者测量值或已观察疗效。"
                        "引用格式：[doc_ref]。不足时明确说明。"
                    ),
                },
            )
        except TimeoutError as exc:
            trace.update(status="unavailable", code="rag_timeout")
            raise DomainError(
                "rag_timeout", "远程知识检索超时。", outcome="unavailable", retryable=True
            ) from exc
        except DomainError as exc:
            trace.update(status=exc.outcome, code=exc.code)
            raise
        finally:
            trace["elapsed_ms"] = (time.monotonic() - started) * 1000
