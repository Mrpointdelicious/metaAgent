"""
创建日期：2026-10-21
文件功能：通过 rag-portfolio 服务取得带发布、页码和哈希的知识证据。

Hospital KB scope is selected by trusted spaceId; medical literature uses global KBs.
No patient IDs,
roles, tokens, or caller-provided KB IDs are sent in the model-controlled payload.
Retrieval, business qualification, reranking and context packing stay in the RAG service.
"""

import hashlib
from datetime import datetime
from uuid import UUID

import httpx
from pydantic import BaseModel, Field, ValidationError

from meta_agent.application.context import RunContext
from meta_agent.contracts import DomainError, TaskResult, TaskSpec
from meta_agent.domains.facts import FactBuilder


class PortfolioEvidence(BaseModel):
    document_id: UUID
    version_id: UUID
    chunk_id: UUID
    release_id: UUID
    title: str = Field(min_length=1, max_length=500)
    section: str
    text: str = Field(min_length=1, max_length=100000)
    source_locator: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    page: int | None = Field(default=None, ge=1)
    anchors: list[dict] = Field(default_factory=list)
    retrieved_at: datetime
    score: float | None = None


class PortfolioResponse(BaseModel):
    evidence: list[PortfolioEvidence] = Field(max_length=20)
    notes: list[str] = Field(default_factory=list)
    trace_id: UUID
    used_context_tokens: int = Field(ge=0)
    decision: str = "accept"
    query_decisions: list[dict] = Field(default_factory=list)
    policy_version: str | None = None


class PortfolioKnowledgeAdapter:
    async def execute(self, task: TaskSpec, args: dict, ctx: RunContext) -> TaskResult:
        settings = ctx.settings
        space_id = getattr(ctx.scope, "space_id", None)
        scene_id = getattr(ctx.scope, "scene_id", None)
        bindings = settings.portfolio_space_kb_ids
        domain = args["domain"]
        if domain == "health":
            kb_ids = settings.portfolio_kb_ids
        elif bindings:
            kb_ids = bindings.get(space_id, {}).get(domain, [])
            if not kb_ids:
                raise DomainError(
                    "knowledge_space_unavailable",
                    "当前 spaceId 未配置此类知识检索，请确认空间信息。",
                    outcome="unavailable",
                )
        else:
            kb_ids = settings.portfolio_kb_ids
        token = settings.portfolio_token.get_secret_value()
        if domain in {"help", "product"} and settings.portfolio_help_token.get_secret_value():
            token = settings.portfolio_help_token.get_secret_value()
        if not token or not kb_ids:
            raise DomainError(
                "knowledge_unavailable", "知识检索服务尚未配置。", outcome="unavailable"
            )
        query = args["query"]
        queries = list(dict.fromkeys(args.get("subqueries") or [query]))
        if not 1 <= len(queries) <= 4 or any(not q.strip() or len(q) > 2000 for q in queries):
            raise DomainError(
                "knowledge_query_too_long", "请缩短检索问题。", outcome="clarification"
            )
        budget = ctx.record.metrics.setdefault(
            "knowledge_budget", {"queries": 0, "calls": {}, "used_context_tokens": 0}
        )
        remaining = settings.portfolio_context_tokens - budget["used_context_tokens"]
        if (
            budget["queries"] + len(queries) > 4
            or budget["calls"].get(domain, 0) >= 2
            or remaining < 128
        ):
            raise DomainError(
                "knowledge_budget_exhausted",
                "本轮检索预算已用完，请根据已有证据回答并说明缺口。",
                outcome="unavailable",
            )
        budget["queries"] += len(queries)
        budget["calls"][domain] = budget["calls"].get(domain, 0) + 1
        # Reserve the remaining context before awaiting, including concurrent domain calls.
        reserved = (
            max(128, remaining // 2)
            if budget["calls"][domain] == 1 and budget["queries"] < 4
            else remaining
        )
        budget["used_context_tokens"] += reserved
        try:
            async with httpx.AsyncClient(timeout=settings.portfolio_timeout_seconds) as client:
                response = await client.post(
                    settings.portfolio_base_url.rstrip("/") + "/v1/retrieve",
                    headers={"Authorization": "Bearer " + token},
                    json={
                        "kb_ids": kb_ids,
                        "queries": [{"q": q} for q in queries],
                        "strategy": "hybrid",
                        "candidate_k": 40,
                        "top_k": settings.portfolio_top_k,
                        "max_context_tokens": reserved,
                        "rerank": True,
                        "answer_mode": "evidence",
                    },
                )
                response.raise_for_status()
                if len(response.content) > settings.max_tool_response_bytes:
                    raise ValueError("oversized_knowledge_response")
                result = PortfolioResponse.model_validate(response.json())
                if any(
                    hashlib.sha256(row.text.encode()).hexdigest() != row.content_hash
                    for row in result.evidence
                ):
                    raise ValueError("knowledge_content_hash_mismatch")
        except (httpx.HTTPError, ValueError, ValidationError) as exc:
            budget["used_context_tokens"] -= reserved
            raise DomainError(
                "knowledge_unavailable", "知识检索暂不可用，请稍后重试。", outcome="unavailable"
            ) from exc
        budget["used_context_tokens"] += min(result.used_context_tokens, reserved) - reserved
        if not result.evidence:
            unavailable = result.decision == "unavailable"
            ctx.record.metrics.setdefault("knowledge_retrieval", []).append(
                {
                    "task_id": task.task_id,
                    "domain": domain,
                    "kb_ids": kb_ids,
                    "space_id": space_id if domain != "health" else None,
                    "scene_id": scene_id if domain != "health" else None,
                    "trace_id": str(result.trace_id),
                    "sources": [],
                    "notes": result.notes,
                    "query_decisions": result.query_decisions,
                    "policy_version": result.policy_version,
                }
            )
            return TaskResult(
                task_id=task.task_id,
                status="unavailable",
                code="knowledge_unavailable"
                if unavailable
                else "low_relevance"
                if "low_relevance" in result.notes
                else "no_knowledge_evidence",
                message="相关性校验暂不可用，请稍后重试。"
                if unavailable
                else "未找到相关性足够的已发布资料，不能据此补充结论。",
                retryable=unavailable,
                outputs={
                    "retrieval_trace_id": str(result.trace_id),
                    "notes": result.notes,
                    "query_decisions": result.query_decisions,
                    "policy_version": result.policy_version,
                },
            )
        snippets, citations = [], []
        for row in result.evidence:
            reference = f"{row.document_id}@{row.version_id}#{row.chunk_id}"
            page = f"PDF 第 {row.page} 页" if row.page else "原文分段"
            citation = {
                "doc_ref": reference,
                "title": row.title,
                "page": row.page,
                "section": row.section,
                "source_locator": row.source_locator,
                "version_id": str(row.version_id),
                "release_id": str(row.release_id),
                "chunk_id": str(row.chunk_id),
                "content_hash": row.content_hash,
                "score": row.score,
            }
            citations.append(citation)
            snippets.append(
                {**citation, "text": row.text, "label": f"{row.title} · {page} · {row.section}"}
            )
        envelope = await ctx.repository.save_evidence(
            ctx.scope.scope_hash,
            "knowledge.search",
            ctx.record.request_id,
            {
                "snippets": snippets,
                "retrieval": result.model_dump(mode="json"),
                "meta": {
                    "watermark": ":".join(sorted({str(r.release_id) for r in result.evidence}))
                },
            },
            "portfolio-evidence-v1",
        )
        builder = FactBuilder(envelope)
        for index, snippet in enumerate(snippets):
            builder.add(
                f"/snippets/{index}/text",
                "knowledge_excerpt",
                snippet["label"],
                required=True,
                uses=["display", "knowledge_explanation"],
            )
        for view in builder.views:
            view.fact.source_type = "document"
        degraded = any(
            note in result.notes
            for note in ("reranker_unavailable_using_fusion_order", "partial_query_coverage")
        )
        ctx.record.metrics.setdefault("knowledge_retrieval", []).append(
            {
                "task_id": task.task_id,
                "space_id": space_id if domain != "health" else None,
                "scene_id": scene_id if domain != "health" else None,
                "domain": domain,
                "kb_ids": kb_ids,
                "trace_id": str(result.trace_id),
                "sources": citations,
                "notes": result.notes,
                "query_decisions": result.query_decisions,
                "policy_version": result.policy_version,
            }
        )
        return TaskResult(
            task_id=task.task_id,
            status="partial" if degraded else "succeeded",
            message="仅部分子问题获得足够证据，其余部分不能据此作答。" if degraded else "",
            evidence_ids=[envelope.evidence_id],
            facts=builder.views,
            outputs={
                "doc_refs": [r["doc_ref"] for r in citations],
                "citations": citations,
                "retrieval_trace_id": str(result.trace_id),
                "notes": result.notes,
                "query_decisions": result.query_decisions,
                "knowledge_budget": budget,
                "citation_instruction": (
                    "引用资料时写明文献标题和 PDF 页码；资料不包含患者实测结论。"
                ),
            },
        )
