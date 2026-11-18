"""
创建日期：2026-11-16
文件功能：功能令牌绑定的独立完整流程基类（固定单一任务，不走 LLM 分类器）。

每个功能分支（ai_summary / ai_medical_order / ...）是一个独立注册的分支：
整条 execute 就是该功能的完整链路（登录、取数、生成、下发都在 pipeline 内
完成），不借用 consultation_room 的分类器与子流程路由。分支注册名即客户端
令牌 flow 的绑定值，会话类型、goal、metrics 都按独立分支记账。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from meta_agent.observability.trace import CURRENT_RUN_ID

if TYPE_CHECKING:
    from meta_agent.application.context import RunContext
    from meta_agent.application.service import ApplicationService

logger = logging.getLogger(__name__)


class FixedTaskFlow:
    """独立功能流程：一条流程 = 一个固定任务管线 + 固定工具边界。

    子类声明 branch / task_name / metrics_key 三个类属性，并实现
    _pipeline(ctx, model) -> str；本基类负责统一记账（goal 状态、指标、
    日志）与 answer_part 事件下发。
    """

    # 分支注册名（必须与 flows/__init__.py 的 branches 键一致）。
    branch: str = ""
    # 任务展示名，用于 goal 标识（如「医嘱生成」）。
    task_name: str = ""
    # 流程完成后的指标键（如 medical_order_filled）。
    metrics_key: str = ""

    def __init__(self, application: ApplicationService) -> None:
        self._application = application

    async def _pipeline(self, ctx: RunContext, model: Any) -> str:
        """执行该功能的完整链路，返回下发给前端的文本；子类实现。"""
        raise NotImplementedError

    async def execute(self, ctx: RunContext) -> None:
        runtime = self._application.agent_runtime
        model = runtime.model if runtime else None
        goal = f"{self.branch}:{self.task_name}"
        run_id_token = CURRENT_RUN_ID.set(ctx.record.run_id)
        try:
            text = await self._pipeline(ctx, model)
            ctx.record.goal_statuses[goal] = "succeeded"
            ctx.record.metrics[self.metrics_key] = True
            logger.info(
                "%s completed",
                self.branch,
                extra={
                    "run_id": ctx.record.run_id,
                    "event_type": f"{self.branch}_completed",
                    "fields": {
                        "client": getattr(getattr(ctx, "scope", None), "client_id", "primary"),
                    },
                },
            )
            await ctx.emitter.emit(
                "answer_part",
                {
                    "text": text,
                    "fact_ids": [],
                    "doc_refs": [],
                    "revision": 1,
                    "replaces": None,
                },
                goal_id=goal,
            )
        finally:
            CURRENT_RUN_ID.reset(run_id_token)
