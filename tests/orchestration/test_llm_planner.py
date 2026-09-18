"""
创建日期：2026-09-03
文件功能：测试 LLMTaskPlanner 的结构化结果转换与类型边界。
"""

import asyncio
from typing import Any, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import RunnableLambda

from meta_agent.orchestration.llm_planner import LLMTaskPlanner
from meta_agent.orchestration.planner import PlannerDecision


class FakePlannerModel:
    """返回预设 PlannerDecision 的测试模型。"""

    def __init__(
        self,
        result: Any,
    ) -> None:
        self._result = result

    def with_structured_output(
        self,
        schema: Any,
        **kwargs: Any,
    ) -> Any:
        async def return_result(_: Any) -> Any:
            return self._result

        return RunnableLambda(return_result)


def test_llm_planner_converts_decision_to_task_plan() -> None:
    decision = PlannerDecision(
        tasks=[
            "session_analysis",
        ],
        requested_output="answer",
    )

    fake_model = FakePlannerModel(result=decision)

    planner = LLMTaskPlanner(
        model=cast(
            BaseChatModel,
            fake_model,
        )
    )

    plan = asyncio.run(planner.plan("分析最近一次训练"))

    assert plan.tasks == ("session_analysis",)

    assert plan.requested_output == "answer"


def test_llm_planner_converts_report_decision() -> None:
    decision = PlannerDecision(
        tasks=[
            "session_analysis",
            "single_report",
        ],
        requested_output="answer_and_report",
    )

    fake_model = FakePlannerModel(result=decision)

    planner = LLMTaskPlanner(
        model=cast(
            BaseChatModel,
            fake_model,
        )
    )

    plan = asyncio.run(planner.plan("分析训练并生成报告"))

    assert plan.tasks == (
        "session_analysis",
        "single_report",
    )

    assert plan.requested_output == "answer_and_report"


def test_llm_planner_rejects_unexpected_result_type() -> None:
    fake_model = FakePlannerModel(
        result={
            "tasks": ["session_analysis"],
            "requested_output": "answer",
        }
    )

    planner = LLMTaskPlanner(
        model=cast(
            BaseChatModel,
            fake_model,
        )
    )

    with pytest.raises(
        TypeError,
        match="非预期的结构化结果",
    ):
        asyncio.run(planner.plan("分析训练"))
