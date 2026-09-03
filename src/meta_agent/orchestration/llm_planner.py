"""
创建日期：2026-09-02
文件功能：使用 LangChain ChatModel 生成结构化领域任务计划。
"""

import logging
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate

from meta_agent.orchestration.planner import (
    PlannerDecision,
    TaskPlan,
)

from meta_agent.prompts.planner import (
    PLANNER_PROMPT,
    PLANNER_PROMPT_VERSION,
)
from langchain_core.language_models.chat_models import BaseChatModel

"""
日志系统
"""

logger = logging.getLogger(__name__)

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

PLANNER_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """
你是康复数据智能体的任务规划器。

你的职责不是回答用户问题，
而是确定完成用户请求所需要执行的最小领域任务集合。

当前允许的领域任务：

patient_context
- 获取患者当前多源数据概览。
- 适用于用户希望了解患者基本情况、
  当前有哪些训练数据或设备数据。

session_analysis
- 分析患者训练会话数据。
- 适用于用户询问训练情况、训练表现、
  指标变化、改善、下降或训练结果。

single_report
- 生成单次训练报告、报表或图表。
- 只有用户明确要求生成报告、报表、
  图片或图表时才使用。

规划规则：

1. 只能从上述任务中选择。
2. 不允许创造新的任务名。
3. 选择完成请求所需的最小任务集合。
4. single_report 必须与 session_analysis 同时存在。
5. 不进行医学诊断。
6. 不直接调用任何具体工具或 API。
7. 不生成面向患者的最终回答。
""".strip(),
        ),
        (
            "human",
            "{query}",
        ),
    ]
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

        plan = TaskPlan(
        tasks=tuple(decision.tasks),
        requested_output=decision.requested_output,
        )
        logger.info(
        "LLM Planner produced task plan: "
        "tasks=%s requested_output=%s prompt_version=%s",
        plan.tasks,
        plan.requested_output,
        PLANNER_PROMPT_VERSION,
        )

        return plan