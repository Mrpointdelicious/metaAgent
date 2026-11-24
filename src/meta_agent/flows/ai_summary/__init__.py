"""
创建日期：2026-11-16
文件功能：AI纪要总结独立流程，与 Dify 工作流「AI纪要总结」逐步对齐。

链路：
  1. 使用随请求提交的对话记录、经核验的本次就诊摘要或当前会话历史。
  2. 时间归一 + 对话整合（flows/ai_summary/textproc.py，对应两个代码节点）：
     timeStamp / 「起止时间」换算成北京时间，语义过滤后与历史累积拼接，
     顺带算出 exceed_six 供分段用。
  3. LLM：用「部分对话总结 + 全部对话总结」提示词生成纪要纯文本。
  4. 后处理（flows/ai_summary/parse.py）：拆成 part / all，最终 answer 就是
     {"part": "...", "all": "..."} 的 JSON 字符串（Dify 侧 summary 变量的契约）。

只读链路：不写病历、医嘱、处方、量表，也不声称已保存或生效。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from meta_agent.contracts import DomainError
from meta_agent.flows.consultation_room.client import ConsultationClient
from meta_agent.flows.consultation_room.medical_record import _extract_content
from meta_agent.flows.fixed_task import FixedTaskFlow

from .parse import apply_time_range, parse_summary, render_summary, strip_zero_time_range
from .summary_prompt import SUMMARY_PART_ALL_PROMPT
from .textproc import (
    integrate,
    normalize_records,
    record_is_dialogue,
    record_span,
    record_text,
    render_transcript,
)

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext

logger = logging.getLogger(__name__)

# 调用方可能把记录数组放在 inputs 的这些键里（query 是首选位置）。
_RECORD_INPUT_KEYS = ("records", "dialogue", "data", "messages", "chat_records")


def _as_records(value: Any) -> list[dict[str, Any]]:
    """把 query / inputs 里的值解析成记录列表；不像记录数组就返回空。"""
    if isinstance(value, (list, dict)):
        return normalize_records(value)
    text = str(value or "").strip()
    if not text:
        return []
    # 只有看起来像数组（或 {"data": [...]}）时才尝试解析，避免把普通问句当 JSON。
    if not (text.startswith("[") or text.startswith('{"')):
        return []
    return normalize_records(text)


async def _load_records(ctx: RunContext) -> tuple[list[dict[str, Any]], str]:
    """按 query → inputs → 院内对话记录的顺序取记录，返回记录与来源标记。"""
    records = _as_records(ctx.query)
    if records:
        return records, "query"
    for key in _RECORD_INPUT_KEYS:
        records = _as_records(ctx.business_inputs.get(key, ""))
        if records:
            return records, f"inputs.{key}"
    encounter = ConsultationClient(ctx, "clinical.summary").encounter_summary()
    records = normalize_records(encounter) or ([{"part": encounter}] if encounter.strip() else [])
    return records, "encounter" if records else "session"


def _history_text(ctx: RunContext) -> str:
    """会话记忆里的历史轮次，作为「全部对话总结」的上下文。"""
    history: list[str] = []
    for turn in ctx.memory.turns:
        user_text = str(turn.get("user") or "").strip()
        assistant_text = str(turn.get("assistant") or "").strip()
        if user_text:
            history.append("患者：" + user_text)
        if assistant_text:
            history.append("医生：" + assistant_text)
    return "\n".join(history)


def _session_part(ctx: RunContext) -> tuple[str, str]:
    """没有院内记录时，用会话记忆拼 arg1/arg2：本轮 query + 之前的历史。"""
    return ctx.query, _history_text(ctx)


class Flow(FixedTaskFlow):
    branch = "ai_summary"
    task_name = "纪要总结"
    metrics_key = "summary_generated"

    async def _pipeline(self, ctx: RunContext, model: Any) -> str:
        if model is None:
            raise DomainError("summary_unavailable", "纪要总结暂不可用。", outcome="unavailable")

        records, source = await _load_records(ctx)
        span = ""
        if any(record_is_dialogue(item) for item in records):
            # 结构化记录（role/timeStamp/from/message）：整批作为本轮对话，
            # 时间区间由记录本身算出，模型只负责归纳。
            arg1 = render_transcript(records)
            arg2 = _history_text(ctx)
            span = record_span(records)
        else:
            segments = [text for text in (record_text(item) for item in records) if text]
            if segments:
                # 院内对话记录只给了整段 part：最后一段当作本轮新增，其余累积。
                arg1, arg2 = segments[-1], "".join(segments[:-1])
            else:
                arg1, arg2 = _session_part(ctx)

        merged = integrate(arg1, arg2)
        ctx.record.metrics["summary_exceed_six"] = merged["exceed_six"]
        ctx.record.metrics["summary_source"] = source
        if span:
            ctx.record.metrics["summary_time_span"] = span

        prompt = SUMMARY_PART_ALL_PROMPT.replace("{{arg1_text}}", merged["arg1_text"]).replace(
            "{{result}}", merged["result"]
        )
        await ctx.llm_budget.take()
        response = await model.ainvoke(
            prompt,
            config={
                "tags": ["ai_summary", "summary"],
                "metadata": {"component": "ai_summary"},
            },
        )
        text = _extract_content(response).strip()
        if not text:
            raise DomainError(
                "summary_empty", "当前对话内容不足，暂时无法生成纪要。", outcome="unavailable"
            )

        summary = parse_summary(text)
        if span:
            # 请求里带着 timeStamp，就以记录的真实区间为准，纠正模型瞎填/占位的时间。
            summary["part"] = apply_time_range(summary["part"], span)
        else:
            # 会话兜底没有真实时间戳，模型会填 00:00:00，这种情况下把区间去掉。
            summary["part"] = strip_zero_time_range(summary["part"])
            summary["all"] = strip_zero_time_range(summary["all"])
        if not summary["part"] and not summary["all"]:
            # 模型没按两段格式输出时不要把空串丢给调用方，原文兜底成「全部总结」。
            logger.warning(
                "summary format not followed, falling back to raw text",
                extra={
                    "run_id": ctx.record.run_id,
                    "event_type": "summary_format_fallback",
                },
            )
            summary = {"part": "", "all": text + "\n"}
        return render_summary(summary)
