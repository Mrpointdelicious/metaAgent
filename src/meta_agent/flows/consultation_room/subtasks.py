"""
创建日期：2026-10-14
文件功能：consultation_room 分支按 category 路由到子流程。

分类器给出 category 后，由 route() 查表分发到对应子流程处理器；每个处理器
只改变 answer_part.text 的内容，输入 / 输出字段与其它分支保持一致。

子流程分组：
  - 基础问答 / 指令功能 → 独立子代理（知识库工具 + 指令工具）
  - 病历填写
  - 邀请医生
  - 处方配置
  - 量表测评
  - 医嘱填写
  - 结束问诊

评估分析暂未使用：分类器仍可识别，但路由落到兜底「基础问答与指令」。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from meta_agent.agent.context import BASIC_COMMAND_SYSTEM_PROMPT
from meta_agent.agent.results import collect_agent_results
from meta_agent.agent.tools import BASIC_COMMAND_TOOLS
from meta_agent.contracts import DomainError
from meta_agent.flows.default.post_process import post_process

from .invite_doctor import run_invite_doctor
from .medical_order import run_medical_order
from .medical_record import run_medical_record
from .prescription import run_prescription
from .scale_assessment import run_scale_assessment

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext
    from meta_agent.application.service import ApplicationService

logger = logging.getLogger(__name__)

Handler = Callable[["RunContext", str, str, "ApplicationService"], Awaitable[None]]


async def _basic_or_command(
    ctx: RunContext, category: str, reason: str, application: ApplicationService
) -> None:
    """基础问答 + 指令：用独立子代理（知识库工具 + 指令工具）作答。"""
    runtime = application.agent_runtime
    if runtime is None:
        raise DomainError(
            "sub_agent_unavailable", "基础问答与指令子代理暂不可用。", outcome="unavailable"
        )
    graph = runtime.sub_agent(
        tool_names=BASIC_COMMAND_TOOLS,
        system_prompt=BASIC_COMMAND_SYSTEM_PROMPT,
        name="basic_command",
    )
    answer = await runtime.execute_sub(
        ctx,
        graph,
        goal_id="consultation_room:基础问答与指令",
        emit_answer=False,
        system_prompt=BASIC_COMMAND_SYSTEM_PROMPT,
    )
    collected = await collect_agent_results(ctx)
    result = post_process(ctx, answer, collected, include_timestamp=False)
    text = result.get("answer") if isinstance(result, dict) else result
    await ctx.emitter.emit(
        "answer_part",
        {
            "text": text,
            "fact_ids": [],
            "doc_refs": [],
            "revision": 1,
            "replaces": None,
        },
        goal_id="consultation_room:基础问答与指令",
    )


async def _medical_record(
    ctx: RunContext, category: str, reason: str, application: ApplicationService
) -> None:
    """病历填写：登录 → 取病历/用户角色/模板 → 取对话记录 → LLM 代写 → 封装事件下发。"""
    runtime = application.agent_runtime
    model = runtime.model if runtime else None
    if model is None:
        raise DomainError(
            "medical_record_model_unavailable", "病历填写模型暂不可用。", outcome="unavailable"
        )

    result = await run_medical_record(ctx, ctx.query, model)

    ctx.record.goal_statuses["consultation_room:病历填写"] = "succeeded"
    ctx.record.metrics["medical_record_filled"] = True
    logger.info(
        "medical record filled",
        extra={
            "run_id": ctx.record.run_id,
            "event_type": "medical_record_filled",
            "fields": {"has_result": bool(result)},
        },
    )
    await ctx.emitter.emit(
        "answer_part",
        {
            "text": result,
            "fact_ids": [],
            "doc_refs": [],
            "revision": 1,
            "replaces": None,
        },
        goal_id="consultation_room:病历填写",
    )


async def _invite_doctor(
    ctx: RunContext, category: str, reason: str, application: ApplicationService
) -> None:
    """邀请医生：登录 → 取在线医生/患者病症 → 有医生走 LLM 推荐，无医生下发固定事件。"""
    runtime = application.agent_runtime
    model = runtime.model if runtime else None

    result = await run_invite_doctor(ctx, model)

    ctx.record.goal_statuses["consultation_room:邀请医生"] = "succeeded"
    ctx.record.metrics["invite_doctor_done"] = True
    logger.info(
        "invite doctor done",
        extra={
            "run_id": ctx.record.run_id,
            "event_type": "invite_doctor_done",
            "fields": {"has_result": bool(result)},
        },
    )
    await ctx.emitter.emit(
        "answer_part",
        {
            "text": result,
            "fact_ids": [],
            "doc_refs": [],
            "revision": 1,
            "replaces": None,
        },
        goal_id="consultation_room:邀请医生",
    )


async def _prescription(
    ctx: RunContext, category: str, reason: str, application: ApplicationService
) -> None:
    """处方配置：意图识别 → 选择添加 / 推荐处方，推荐结果累计写入会话变量。"""
    runtime = application.agent_runtime
    model = runtime.model if runtime else None

    result = await run_prescription(ctx, ctx.query, model)

    ctx.record.goal_statuses["consultation_room:处方配置"] = "succeeded"
    ctx.record.metrics["prescription_done"] = True
    logger.info(
        "prescription done",
        extra={
            "run_id": ctx.record.run_id,
            "event_type": "prescription_done",
            "fields": {"has_result": bool(result)},
        },
    )
    await ctx.emitter.emit(
        "answer_part",
        {
            "text": result,
            "fact_ids": [],
            "doc_refs": [],
            "revision": 1,
            "replaces": None,
        },
        goal_id="consultation_room:处方配置",
    )


async def _scale_assessment(
    ctx: RunContext, category: str, reason: str, application: ApplicationService
) -> None:
    """量表测评：意图识别 → 选择添加 / 推荐量表，推荐结果累计写入会话变量。"""
    runtime = application.agent_runtime
    model = runtime.model if runtime else None

    result = await run_scale_assessment(ctx, ctx.query, model)

    ctx.record.goal_statuses["consultation_room:量表测评"] = "succeeded"
    ctx.record.metrics["scale_assessment_done"] = True
    logger.info(
        "scale assessment done",
        extra={
            "run_id": ctx.record.run_id,
            "event_type": "scale_assessment_done",
            "fields": {"has_result": bool(result)},
        },
    )
    await ctx.emitter.emit(
        "answer_part",
        {
            "text": result,
            "fact_ids": [],
            "doc_refs": [],
            "revision": 1,
            "replaces": None,
        },
        goal_id="consultation_room:量表测评",
    )


async def _medical_order(
    ctx: RunContext, category: str, reason: str, application: ApplicationService
) -> None:
    """医嘱填写：登录 → 取病历/对话记录 → LLM 生成康复医嘱 → 封装事件下发。"""
    runtime = application.agent_runtime
    model = runtime.model if runtime else None

    result = await run_medical_order(ctx, ctx.query, model)

    ctx.record.goal_statuses["consultation_room:医嘱填写"] = "succeeded"
    ctx.record.metrics["medical_order_filled"] = True
    logger.info(
        "medical order filled",
        extra={
            "run_id": ctx.record.run_id,
            "event_type": "medical_order_filled",
            "fields": {"has_result": bool(result)},
        },
    )
    await ctx.emitter.emit(
        "answer_part",
        {
            "text": result,
            "fact_ids": [],
            "doc_refs": [],
            "revision": 1,
            "replaces": None,
        },
        goal_id="consultation_room:医嘱填写",
    )


_END_CONSULTATION_RESULT = json.dumps(
    {
        "event": "end_consultation",
        "display_text": "[Answer:]是否结束本次问诊？\n",
        "payload": {},
    },
    ensure_ascii=False,
)


async def _end_consultation(
    ctx: RunContext, category: str, reason: str, application: ApplicationService
) -> None:
    """结束问诊：固定下发「是否结束本次问诊？」确认事件。"""
    ctx.record.goal_statuses["consultation_room:结束问诊"] = "succeeded"
    ctx.record.metrics["end_consultation_done"] = True
    logger.info(
        "end consultation emitted",
        extra={
            "run_id": ctx.record.run_id,
            "event_type": "end_consultation_done",
        },
    )
    await ctx.emitter.emit(
        "answer_part",
        {
            "text": _END_CONSULTATION_RESULT,
            "fact_ids": [],
            "doc_refs": [],
            "revision": 1,
            "replaces": None,
        },
        goal_id="consultation_room:结束问诊",
    )

# category -> 子流程处理器。评估分析暂未使用，不在此映射，由 route() 兜底。
HANDLERS: dict[str, Handler] = {
    "基础问答": _basic_or_command,
    "指令功能": _basic_or_command,
    "病历填写": _medical_record,
    "邀请医生": _invite_doctor,
    "处方配置": _prescription,
    "量表测评": _scale_assessment,
    "医嘱填写": _medical_order,
    "结束问诊": _end_consultation,
}


async def route(
    ctx: RunContext, category: str, reason: str, application: ApplicationService
) -> None:
    """按分类结果分发到对应子流程；未知类别兜底到基础问答与指令。"""
    handler = HANDLERS.get(category) or _basic_or_command
    ctx.record.metrics["subtask"] = category
    await handler(ctx, category, reason, application)
