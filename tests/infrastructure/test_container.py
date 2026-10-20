"""
创建日期：2026-09-03
文件功能：测试 Container 的 Planner 策略创建与依赖装配。
"""

from typing import Any

import pytest
from langchain_core.runnables import RunnableLambda
from pydantic import SecretStr

from meta_agent.config import Settings
from meta_agent.contracts import IntentDecision
from meta_agent.infrastructure.container import (
    create_task_planner,
)
from meta_agent.planning.parser import ConservativePlanner, StructuredIntentPlanner
from tests.helpers.settings import AppTestSettings


def test_uvicorn_custom_loop_runs_asyncio():
    import asyncio

    from uvicorn import Config

    factory = Config(
        "meta_agent.app:app", loop="meta_agent.infrastructure.event_loop:loop_factory"
    ).get_loop_factory()

    async def check():
        assert isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)

    asyncio.run(check(), loop_factory=factory)


class FakePlannerModel:
    """供 Container 测试使用的最小 LangChain Model 替身。"""

    def with_structured_output(
        self,
        schema: Any,
        **kwargs: Any,
    ) -> Any:
        async def return_decision(
            _: Any,
        ) -> IntentDecision:
            return IntentDecision(decision="respond")

        return RunnableLambda(return_decision)


def test_create_deterministic_task_planner() -> None:
    settings = Settings(planner_mode="deterministic")

    planner = create_task_planner(settings)

    assert isinstance(
        planner,
        ConservativePlanner,
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
        "meta_agent.infrastructure.container.create_planner_model",
        fake_create_planner_model,
    )

    settings = AppTestSettings(
        planner_mode="llm",
        deepseek_api_key=SecretStr("test-key"),
    )

    planner = create_task_planner(settings)

    assert isinstance(
        planner,
        StructuredIntentPlanner,
    )
