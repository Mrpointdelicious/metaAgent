"""
创建日期：2026-11-16
文件功能：AI医嘱生成独立流程，与 Dify 工作流「AI医嘱生成」逐步对齐。

链路（四段，全部在 query 上完成，不再反查院内接口）：
  1. 代码 A（transcript.conversation_text）：记录数组 → `医生(医生1)：…` 对话文本；
  2. 条件判断：文本里既没有「医生」也没有「患者」→ 固定输出「无问诊记录」三条；
  3. LLM（medical_order_prompt）：严格按文本抽取，产出
     {"ChiefComplaint": …, "Diagnosis": …, "Management": …}；
  4. 代码 B（transcript.to_json_text / build_order_json）：去围栏、中文与单引号统一成
     英文双引号、修结构位中文标点，最终 answer 就是这三字段的纯 JSON 对象文本
     （不套 {"data": …} 外壳，与 Dify 最终输出一致）。

只产出结构化草稿，不写病历、不落医嘱，也不声称已保存或生效。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from meta_agent.contracts import DomainError
from meta_agent.flows.consultation_room.medical_record import _extract_content
from meta_agent.flows.fixed_task import FixedTaskFlow

from .medical_order_prompt import build_prompt
from .transcript import build_order_json, conversation_text, has_dialogue_roles

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext

logger = logging.getLogger(__name__)

# 没有医患对话时的固定输出（Dify 条件分支原文）。
NO_RECORD_JSON = (
    '{\n"ChiefComplaint": "无问诊记录",\n"Diagnosis": "无问诊记录",\n"Management": "无问诊记录"\n}'
)


async def run_medical_order(ctx: RunContext, query: str, model: Any) -> str:
    """执行医嘱生成链路，返回 {"ChiefComplaint","Diagnosis","Management"} 的 JSON 文本。"""
    conversation = conversation_text(query)
    ctx.record.metrics["medical_order_chars"] = len(conversation)

    if not has_dialogue_roles(conversation):
        # 空输入、纯文本闲聊、或记录里角色未知——一律按「无问诊记录」返回，
        # 不调用模型，避免它凭空编出主诉与诊断。
        ctx.record.metrics["medical_order_source"] = "no_record"
        logger.info(
            "medical order skipped, no doctor/patient in conversation",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "medical_order_no_record",
                "fields": {"chars": len(conversation)},
            },
        )
        return NO_RECORD_JSON

    if model is None:
        raise DomainError(
            "medical_order_model_unavailable", "医嘱生成模型暂不可用。", outcome="unavailable"
        )

    ctx.record.metrics["medical_order_source"] = "llm"
    await ctx.llm_budget.take()
    response = await model.ainvoke(
        build_prompt(conversation),
        config={
            "tags": ["ai_medical_order", "medical_order"],
            "metadata": {"component": "ai_medical_order"},
        },
    )
    text = _extract_content(response).strip()
    if not text:
        raise DomainError(
            "medical_order_empty", "当前对话内容不足，暂时无法生成医嘱。", outcome="unavailable"
        )

    # 代码 B：去围栏 → 引号统一 → 结构位中文标点修正，
    # 并规整成 {ChiefComplaint, Diagnosis, Management} 的纯 JSON 对象文本。
    answer, parsed = build_order_json(text)
    ctx.record.metrics["medical_order_json"] = "ok" if parsed else "raw"
    if not parsed:
        # 模型没给出合法 JSON（极少见）：原样返回文本，交给调用方判断，
        # 不在这里编造字段，也不把错误文本包成 JSON 掩过去。
        logger.warning(
            "medical order output is not a json object",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "medical_order_unparsed",
                "fields": {"chars": len(text)},
            },
        )
    return answer


class Flow(FixedTaskFlow):
    branch = "ai_medical_order"
    task_name = "医嘱生成"
    metrics_key = "medical_order_filled"

    async def _pipeline(self, ctx: RunContext, model: Any) -> str:
        return await run_medical_order(ctx, ctx.query, model)
