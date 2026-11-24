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
from meta_agent.orchestration.capabilities import require_for_run

from .classifier import parse_classification
from .prompts import CATEGORIES, CLASSIFIER_PROMPT
from .subtasks import route

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext
    from meta_agent.application.service import ApplicationService

logger = logging.getLogger(__name__)


class Flow:
    """独立问诊会话：任务分类 → 按类别执行对应处理器。

    goal_root 是 goal 记账键：问诊分支用默认值 consultation_room；
    ai_consultation 分支复用本实现但以自己的名字记账。
    """

    def __init__(
        self, application: ApplicationService, goal_root: str = "consultation_room"
    ) -> None:
        self._application = application
        self._goal_root = goal_root

    async def execute(self, ctx: RunContext) -> None:
        require_for_run(ctx, "clinical.basic")
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
        logger.info(
            "consultation room classified",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "consultation_room_classified",
                "fields": {"category": category},
            },
        )

        # 分类结果不直接输出，只用来选择子流程；由子流程产出 answer_part.text。
        # 子流程全部成功后才把本分支标记为 succeeded，否则失败会保留
        # DomainError 的 outcome（如 unavailable），避免出现「状态 succeeded 但 task_failed」。
        run_id_token = CURRENT_RUN_ID.set(ctx.record.run_id)
        try:
            await route(ctx, category, reason, self._application)
            ctx.record.goal_statuses[self._goal_root] = (
                ctx.record.status if ctx.record.status != "running" else "succeeded"
            )
        finally:
            CURRENT_RUN_ID.reset(run_id_token)
