"""
创建日期：2026-10-25
文件功能：将公共知识契约结果适配为 Agent 证据、引用和工具结果。
"""

import httpx
from jsonschema import Draft202012Validator, ValidationError

from meta_agent.contracts import DomainError, TaskResult
from meta_agent.domains.facts import FactBuilder
from meta_agent.integrations.knowledge import (
    KnowledgeAccessDenied,
    KnowledgeContractError,
    search,
    tool_schema,
)


class RemoteKnowledgeAdapter:
    async def execute(self, task, args, ctx):
        sources = ctx.settings.knowledge_sources
        try:
            Draft202012Validator(tool_schema(sources)).validate(args)
        except ValidationError as exc:
            raise DomainError(
                "invalid_knowledge_request",
                "请使用已配置的知识源和领域，并提供检索问题。",
                outcome="clarification",
            ) from exc
        domain = args.get("domain", sources[0].domains[0])
        source = next(
            (
                item
                for item in sources
                if domain in item.domains and (not args.get("source") or item.id == args["source"])
            ),
            None,
        )
        if source is None:
            raise DomainError(
                "knowledge_source_unavailable", "所选知识源不支持这个领域。", outcome="unsupported"
            )
        scope = {}
        if domain in source.scoped_domains:
            space = ctx.scope.space_id
            if not space:
                raise DomainError(
                    "knowledge_space_required", "请先确认当前空间。", outcome="clarification"
                )
            scope["space_id"] = space
        payload = {
            "query": args["query"],
            "domain": domain,
            "scope": scope,
            "limits": {"max_context_tokens": ctx.settings.knowledge_context_tokens},
        }
        try:
            async with ctx.tool_slot():
                result = await search(
                    source,
                    payload,
                    request_id=str(ctx.record.request_id),
                    max_bytes=ctx.settings.max_tool_response_bytes,
                )
        except KnowledgeAccessDenied as exc:
            raise DomainError(
                "knowledge_access_denied",
                "当前身份无权访问该知识范围。",
                outcome="unsupported",
            ) from exc
        except httpx.HTTPStatusError as exc:
            denied = exc.response.status_code == 403
            raise DomainError(
                "knowledge_access_denied" if denied else "knowledge_unavailable",
                "当前身份无权访问该知识范围。" if denied else "知识检索暂不可用，请稍后重试。",
                outcome="unsupported" if denied else "unavailable",
                retryable=exc.response.status_code in (429, 502, 503, 504),
            ) from exc
        except httpx.RequestError as exc:
            raise DomainError(
                "knowledge_unavailable",
                "知识检索暂不可用，请稍后重试。",
                outcome="unavailable",
                retryable=True,
            ) from exc
        except (ValueError, ValidationError, KnowledgeContractError) as exc:
            raise DomainError(
                "knowledge_unavailable", "知识检索暂不可用，请稍后重试。", outcome="unavailable"
            ) from exc
        citations = [
            {**row, "doc_ref": row["ref"], "section": row["locator"]} for row in result["sources"]
        ]
        ctx.record.metrics.setdefault("knowledge_retrieval", []).append(
            {
                "source_id": source.id,
                "domain": domain,
                "trace_id": result["trace_id"],
                "status": result["status"],
                "sources": citations,
            }
        )
        outputs = {
            "source_id": source.id,
            "retrieval_trace_id": result["trace_id"],
            "citations": citations,
            "doc_refs": [row["ref"] for row in result["sources"]],
            "gaps": result["gaps"],
        }
        if result["status"] == "no_evidence":
            return TaskResult(
                task_id=task.task_id,
                status="unavailable",
                code="no_knowledge_evidence",
                message="未找到足够的已发布证据，请说明资料缺口。",
                outputs=outputs,
            )
        evidence = await ctx.repository.save_evidence(
            ctx.scope.scope_hash,
            "knowledge.search",
            ctx.record.request_id,
            result,
            "knowledge-search-v1",
        )
        # One generic evidence projection also supports the legacy answer composer.
        builder = FactBuilder(evidence)
        builder.add(
            "/context",
            "knowledge_excerpt",
            source.description,
            required=True,
            uses=["display", "knowledge_explanation"],
        )
        for view in builder.views:
            view.fact.source_type = "document"
        return TaskResult(
            task_id=task.task_id,
            status="partial" if result["status"] == "partial" else "succeeded",
            message="；".join(result["gaps"]),
            evidence_ids=[evidence.evidence_id],
            facts=builder.views,
            outputs=outputs,
        )
