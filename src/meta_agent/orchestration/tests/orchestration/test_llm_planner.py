"""
创建日期：2026-08-29
文件功能：使用 LangChain ChatModel 生成结构化领域任务计划。
"""

from langchain_core.language_models.chat_models import BaseChatModel

from meta_agent.orchestration.planner import (
    PlannerDecision,
    TaskPlan,
)
from meta_agent.prompts.planner import (
    PLANNER_PROMPT,
    PLANNER_PROMPT_VERSION,
)


class LLMTaskPlanner:
    """基于 LangChain ChatModel 的领域任务规划器。"""

    def __init__(
        self,
        model: BaseChatModel,
    ) -> None:
        structured_model = model.with_structured_output(
            PlannerDecision,
            method="function_calling",
        )

        self._chain = (
            PLANNER_PROMPT
            | structured_model
        )

    async def plan(
        self,
        query: str,
    ) -> TaskPlan:
        decision = await self._chain.ainvoke(
            {
                "query": query,
            },
            config={
                "tags": [
                    "planner",
                    "llm",
                ],
                "metadata": {
                    "component": "llm_task_planner",
                    "prompt_version": PLANNER_PROMPT_VERSION,
                },
            },
        )

        if not isinstance(
            decision,
            PlannerDecision,
        ):
            raise TypeError(
                "LLM Planner 返回了非预期的结构化结果："
                f"{type(decision).__name__}"
            )

        return TaskPlan(
            tasks=tuple(
                decision.tasks
            ),
            requested_output=(
                decision.requested_output
            ),
        )