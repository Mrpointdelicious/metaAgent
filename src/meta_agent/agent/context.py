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
    if body.get("tool_name") == "search_knowledge" and body.get("facts"):
        return compact_knowledge(body, budget)
    if body.get("business_view"):
        if estimate_tokens(body) <= budget:
            return body
        result = {
            **body,
            "facts": [],
            "context_truncated": True,
            "patient_message": "部分完整临床字段超出预算，省略的正文不能解释，请缩小查询。",
        }
        for fact in body.get("facts", []):
            result["facts"].append(fact)
            if estimate_tokens(result) > budget:
                result["facts"].pop()
        if estimate_tokens(result) > budget or not result["facts"]:
            return {
                "tool_name": body["tool_name"],
                "evidence_id": body["evidence_id"],
                "business_view": True,
                "context_truncated": True,
                "status": "unavailable",
                "patient_message": "完整临床字段超出预算，请缩小范围；不能据此认为没有记录。",
            }
        return result
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


def compact_knowledge(body: dict, budget: int) -> dict:
    """Keep document excerpts atomic, paired with complete source references."""
    if estimate_tokens(body) <= budget:
        return body
    data = body.get("data") or {}
    refs = data.get("doc_refs", [])
    result = {
        "tool_name": body["tool_name"],
        "status": body.get("status"),
        "evidence_id": body.get("evidence_id"),
        "context_truncated": True,
        "patient_message": "部分原文超出上下文预算，未作句中截断；请按来源或维度缩小查询。",
        "facts": [],
        "data": {"doc_refs": [], "citations": []},
    }
    if data.get("arguments"):
        result["data"]["arguments"] = data["arguments"]
    if data.get("boundary"):
        result["data"]["boundary"] = data["boundary"]
    citations = {s["doc_ref"]: s for s in data.get("citations", [])}
    for index, fact in enumerate(body["facts"]):
        ref = fact.get("doc_ref") or (refs[index] if index < len(refs) else None)
        if not ref:
            continue
        source = {
            k: v
            for k, v in citations.get(ref, {}).items()
            if k
            in {"doc_ref", "title", "source", "document_version", "section", "page", "start", "end"}
        }
        result["facts"].append(fact)
        result["data"]["doc_refs"].append(ref)
        result["data"]["citations"].append(source)
        if estimate_tokens(result) > budget:
            result["facts"].pop()
            result["data"]["doc_refs"].pop()
            result["data"]["citations"].pop()
    if not result["facts"]:
        return {
            "tool_name": body["tool_name"],
            "status": "unavailable",
            "code": "knowledge_context_overflow",
            "evidence_id": body.get("evidence_id"),
            "context_truncated": True,
            "patient_message": "没有完整知识片段能放入本轮预算，请按来源或维度缩小查询。",
        }
    return result


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
            expired = {
                "tool_name": payload.get("tool_name"),
                "status": "expired",
                "patient_message": "该查询证据已过期，请重新查询；旧记录不能作为当前事实。",
            }
            args = (payload.get("data") or {}).get("arguments", {})
            if payload.get("tool_name") == "search_knowledge" and isinstance(args, dict):
                hint = {k: args[k] for k in ("query", "domain") if k in args}
                if hint:
                    expired["query_hint"] = hint
            message.content = json.dumps(expired, ensure_ascii=False)
    return bounded_history(messages, ctx.settings.agent_history_tokens)
