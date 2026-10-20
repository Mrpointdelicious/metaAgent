"""
创建日期：2026-10-05
文件功能：压缩患者上下文，保留工具消息配对并检查证据时效。
"""

import json
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage, messages_to_dict

from meta_agent.context.budget import estimate_tokens


def compact_result(body: dict, budget: int) -> dict:
    """Keep an explicit truncation marker; full data remains in the evidence store."""
    omitted = False

    def shrink(value: Any, text_limit: int, list_limit: int):
        nonlocal omitted
        if isinstance(value, str) and len(value) > text_limit:
            omitted = True
            return value[:text_limit] + "（摘要截断，可缩小查询范围获取更多内容）"
        if isinstance(value, list):
            if len(value) > list_limit:
                omitted = True
            return [shrink(x, text_limit, list_limit) for x in value[:list_limit]]
        if isinstance(value, dict):
            return {
                k: shrink(v, text_limit, list_limit)
                for k, v in value.items()
                if k not in {"display_name_private", "phone", "password"}
            }
        return value

    for text_limit, list_limit in [(1600, 10), (800, 5), (400, 3), (200, 2), (100, 1)]:
        result = shrink(body, text_limit, list_limit)
        if omitted:
            result["context_truncated"] = True
        if estimate_tokens(result) <= budget:
            return result
    return {
        "tool_name": body.get("tool_name"),
        "status": body.get("status"),
        "evidence_id": body.get("evidence_id"),
        "context_truncated": True,
        "patient_message": "查询结果过大，请减少page_size或缩小范围后重查。",
    }


def turn_groups(messages: list[BaseMessage]) -> list[list[BaseMessage]]:
    groups: list[list[BaseMessage]] = []
    for message in messages:
        if isinstance(message, HumanMessage) or not groups:
            groups.append([])
        groups[-1].append(message)
    return groups


def bounded_history(messages: list[BaseMessage], budget: int) -> list[BaseMessage]:
    groups = turn_groups(messages)[-6:]
    while (
        len(groups) > 1
        and estimate_tokens(messages_to_dict([m for g in groups for m in g])) > budget
    ):
        groups.pop(0)
    return [m for group in groups for m in group]


async def fresh_history(messages: list[BaseMessage], ctx) -> list[BaseMessage]:
    for message in messages:
        if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
            continue
        try:
            payload = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        eid = payload.get("evidence_id") if isinstance(payload, dict) else None
        if eid and await ctx.repository.evidence(ctx.scope.scope_hash, eid) is None:
            message.content = json.dumps(
                {
                    "tool_name": payload.get("tool_name"),
                    "status": "expired",
                    "patient_message": "该查询证据已过期，请重新查询；旧记录不能作为当前事实。",
                },
                ensure_ascii=False,
            )
    return bounded_history(messages, ctx.settings.agent_history_tokens)
