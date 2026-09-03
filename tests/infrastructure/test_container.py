"""
创建日期：2026-09-03
文件功能：测试 Container 的 Planner 策略创建与依赖装配。
"""

from typing import Any

import pytest
from langchain_core.runnables import RunnableLambda
from pydantic import SecretStr

from meta_agent.config import Settings
from meta_agent.infrastructure.container import (
    create_task_planner,
)
from meta_agent.orchestration.llm_planner import (
    LLMTaskPlanner,
)
from meta_agent.orchestration.planner import (
    DeterministicTaskPlanner,
    PlannerDecision,
)
from meta_agent.config import Settings
from tests.helpers.settings import TestSettings

class FakePlannerModel:
    """供 Container 测试使用的最小 LangChain Model 替身。"""

    def with_structured_output(
        self,
        schema: Any,
        **kwargs: Any,
    ) -> Any:
        async def return_decision(
            _: Any,
        ) -> PlannerDecision:
            return PlannerDecision(
                tasks=[
                    "session_analysis",
                ],
                requested_output="answer",
            )

        return RunnableLambda(
            return_decision
        )


def test_create_deterministic_task_planner() -> None:
    settings = Settings(
        planner_mode="deterministic"
    )

    planner = create_task_planner(
        settings
    )

    assert isinstance(
        planner,
        DeterministicTaskPlanner,
    )


def test_create_llm_task_planner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_model = FakePlannerModel()

    def fake_create_planner_model(
        settings: Settings,
    ) -> FakePlannerModel:
        return fake_model

    monkeypatch.setattr(
        "meta_agent.infrastructure.container."
        "create_planner_model",
        fake_create_planner_model,
    )

    settings = TestSettings(
        planner_mode="llm",
        deepseek_api_key=SecretStr(
            "test-key"
        ),
    )

    planner = create_task_planner(
        settings
    )

    assert isinstance(
        planner,
        LLMTaskPlanner,
    )