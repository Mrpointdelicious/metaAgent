"""
创建日期：2026-10-10
文件功能：从当前患者的可信训练历史校验UI选择记录，并建立作用域内的会话锚点。
"""

import asyncio
import json
import re

from meta_agent.contracts import DomainError, RecordAnchor


def conversational_only(query: str) -> bool:
    return bool(
        re.fullmatch(r"(?:你好|您好|嗨|谢谢|感谢|再见|hello|hi)[！!。\.\s]*", query.strip(), re.I)
    )


def validate_natural_answer(text: str) -> None:
    if re.search(
        r"https?://|\[(?:answer|image|mode)\]|\[(?:Telepor|ScenePoint|ClickPoint|GamePoint):\]|```",
        text,
        re.I,
    ):
        raise DomainError("answer_not_displayable", "本轮文字回答包含非文字结果，请重新提问。")
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return
    if isinstance(value, (dict, list)):
        raise DomainError("answer_not_displayable", "本轮未形成自然语言回答，请重新提问。")


async def prepare_frontend_context(ctx) -> None:
    ref = ctx.frontend_context.get("selected_session_ref")
    if not ref or conversational_only(ctx.query):
        if (
            ctx.frontend_context
            and re.search(r"上一次|前一次", ctx.query)
            and not ctx.memory.current_record
        ):
            raise DomainError(
                "anchor_missing", "请先明确当前训练记录，再查询上一次。", outcome="clarification"
            )
        return
    # The client reference is untrusted until the patient-scoped history confirms it.
    from meta_agent.agent.tools import _read

    domains = (
        ["iretour"]
        if ref.startswith("iretour_s_v1_")
        else ["irego"]
        if ref.startswith("irego_s_v1_")
        else ["irego", "iretour"]
    )
    await ctx.events.progress("resolving_context")
    for domain in domains:
        for page in range(1, 5):
            if ctx.tool_calls >= ctx.settings.max_tool_calls - 1:
                break
            async with asyncio.timeout(min(ctx.remaining, ctx.settings.tool_timeout_seconds)):
                body, evidence, _ = await _read(
                    ctx,
                    f"get_{domain}_patient_history",
                    {
                        "page_number": page,
                        "page_size": 50,
                        "record_scope": "all",
                    },
                )
            if body.get("status") not in {"success", "available", "partial"}:
                break
            data = body.get("data") or {}
            for item in data.get("items", []):
                if item.get("session_ref") == ref:
                    ctx.memory.current_record = RecordAnchor(
                        domain=domain,
                        session_ref=ref,
                        evidence_id=evidence.evidence_id,
                        source_version=evidence.source_version,
                        session_time=item.get("session_time"),
                    )
                    ctx.memory.history_domain = domain
                    ctx.frontend_context["selected_record_domain"] = domain
                    return
            if not data.get("page", {}).get("has_next"):
                break
    raise DomainError(
        "selected_record_unverified",
        "无法确认所选记录属于当前患者，请重新选择记录。",
        outcome="clarification",
    )
