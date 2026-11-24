"""
创建日期：2026-11-18
文件功能：AI问诊流程的知识检索 —— 直连 MaxKB（knowledge_sources 统一契约）。

与 Dify「AI诊室(测试)」的差异：原流程用 Dify 内置数据集（公司产品/错误码）
与中台 richtxt 企业知识；这里统一改为 MaxKB 已发布资料（catalog 白名单 +
hit_test），检索失败降级为无知识命中（LLM 按自有知识回答），不阻断流程。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx
from jsonschema import ValidationError

from meta_agent.integrations.knowledge import (
    KnowledgeAccessDenied,
    KnowledgeContractError,
    sources_for_scope,
)
from meta_agent.integrations.knowledge import (
    search as knowledge_search,
)

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext

logger = logging.getLogger(__name__)


async def search_maxkb(
    ctx: RunContext, query: str, domain: str = "product"
) -> dict[str, Any] | None:
    """调用配置的首个知识源（MaxKB）检索；不可用时返回 None 并告警。

    返回 knowledge-search-v1 契约结果：{status, context, sources, gaps, trace_id}。
    """
    scope = getattr(ctx, "scope", None)
    configured = getattr(ctx.settings, "knowledge_sources", ())
    sources = sources_for_scope(configured, scope) if scope else []
    if not sources:
        logger.warning(
            "ai_consultation knowledge skipped, no knowledge source configured",
            extra={"run_id": ctx.record.run_id, "event_type": "consultation_knowledge_skipped"},
        )
        return None
    space = getattr(ctx.scope, "space_id", "") or ""
    if not space:
        logger.warning(
            "ai_consultation knowledge skipped, space missing",
            extra={"run_id": ctx.record.run_id, "event_type": "consultation_knowledge_skipped"},
        )
        return None
    source = sources[0]
    payload = {
        "query": query,
        "domain": domain,
        "scope": {"space_id": space},
        "limits": {"max_context_tokens": ctx.settings.knowledge_context_tokens},
    }
    try:
        return await knowledge_search(
            source,
            payload,
            request_id=str(ctx.record.request_id),
            max_bytes=ctx.settings.max_tool_response_bytes,
            role=getattr(scope, "role", None),
        )
    except KnowledgeAccessDenied as exc:
        logger.warning(
            "ai_consultation knowledge access denied, continuing without evidence",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "consultation_knowledge_unavailable",
                "fields": {"code": exc.code if hasattr(exc, "code") else str(exc)},
            },
        )
        return None
    except (
        KnowledgeContractError,
        httpx.HTTPStatusError,
        httpx.RequestError,
        ValidationError,
        ValueError,
    ) as exc:
        logger.warning(
            "ai_consultation knowledge unavailable, continuing without evidence",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "consultation_knowledge_unavailable",
                "fields": {"error": type(exc).__name__},
            },
        )
        return None
