"""
创建日期：2026-10-10
文件功能：收集单 Agent 循环中每一轮工具调用及原始契约，供后处理层消费。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext


async def collect_agent_results(ctx: RunContext) -> list[dict[str, Any]]:
    """把本轮 agent 选中的工具调用（含原始后端契约）收集成列表。

    每条字段：
      task_id / tool / status / message / arguments / facts / raw
    其中 raw 是后端返回的原始契约（evidence.payload），facts 是已由领域
    适配器投影出的结构化事实（仅 doctors/knowledge/hospital/scene 有）。
    """
    items: list[dict[str, Any]] = []
    for tid, result in ctx.record.results.items():
        raw = None
        if result.evidence_ids:
            evidence = await ctx.repository.evidence(
                ctx.scope.scope_hash, result.evidence_ids[0]
            )
            raw = evidence.payload if evidence else None
        items.append(
            {
                "task_id": tid,
                "tool": result.outputs.get("tool_name", tid),
                "status": result.status,
                "message": result.message,
                "arguments": result.outputs.get("arguments"),
                "facts": [f.model_dump(mode="json") for f in result.facts],
                "raw": raw,
            }
        )
    return items
