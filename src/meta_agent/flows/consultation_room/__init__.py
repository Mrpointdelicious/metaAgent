"""
创建日期：2026-10-10
文件功能：pos 包含连续「问诊房间」时的独立问诊流程。

进入后先用一个 LLM 任务分类器把本轮话术归入 9 个任务类别，
再用 classifier.parse_classification 稳健解析出 category / reason，最后按 category
路由到 subtasks 里的对应子流程。分类结果不直接下发，只用于选择子流程。

基础问答/指令使用受限 Agent；临床分支执行原有业务适配器。
结束问诊返回前端确认事件；评估分析尚未启用，明确返回 unsupported。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from meta_agent.contracts import DomainError
from meta_agent.observability.trace import CURRENT_RUN_ID

from .classifier import parse_classification
from .prompts import CATEGORIES, CLASSIFIER_PROMPT
from .subtasks import route

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext
    from meta_agent.application.service import ApplicationService

logger = logging.getLogger(__name__)


class Flow:
    """独立问诊会话：任务分类 → 按类别执行对应处理器。"""

    def __init__(self, application: ApplicationService) -> None:
        self._application = application

    async def execute(self, ctx: RunContext) -> None:
        model = self._application.agent_runtime.model if self._application.agent_runtime else None
        if model is None:
            raise DomainError(
                "classifier_unavailable", "任务分类器暂不可用。", outcome="unavailable"
            )
        await ctx.llm_budget.take()
        prompt = CLASSIFIER_PROMPT.replace("{{user_input}}", ctx.query).replace(
            "{{previous_category}}", ctx.memory.previous_category or ""
        )
        response = await model.ainvoke(
            prompt,
            config={
                "tags": ["consultation_room", "classifier"],
                "metadata": {"component": "consultation_room_classifier"},
            },
        )
        content = response.content if hasattr(response, "content") else str(response)
        if isinstance(content, list):
            content = "\n".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        parsed = parse_classification(content)
        category = parsed["category"].strip()
        reason = parsed["reason"].strip()
        if category not in CATEGORIES:
            category = "基础问答"

        ctx.memory.previous_category = category
        ctx.record.metrics["task_category"] = category
        ctx.record.metrics["task_category_reason"] = reason
        logger.info(
            "consultation room classified",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "consultation_room_classified",
                "fields": {"category": category},
            },
        )

        # 分类结果不直接输出，只用来选择子流程；由子流程产出 answer_part.text。
        # 子流程全部成功后才把 consultation_room 标记为 succeeded，否则失败会保留
        # DomainError 的 outcome（如 unavailable），避免出现「状态 succeeded 但 task_failed」。
        run_id_token = CURRENT_RUN_ID.set(ctx.record.run_id)
        try:
            await route(ctx, category, reason, self._application)
            ctx.record.goal_statuses["consultation_room"] = (
                ctx.record.status if ctx.record.status != "running" else "succeeded"
            )
        finally:
            CURRENT_RUN_ID.reset(run_id_token)
