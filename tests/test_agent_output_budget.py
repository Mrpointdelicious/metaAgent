"""
创建日期：2026-10-25
文件功能：验证Agent独立输出配置、模型接线、上下文预留和截断事件状态。
"""

import asyncio

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from meta_agent.llm.models import create_planner_model
from tests.helpers.runtime import answers, request, runtime
from tests.helpers.settings import AppTestSettings


class BoundFakeModel(GenericFakeChatModel):
    def bind_tools(self, *args, **kwargs):
        return self


@pytest.fixture(autouse=True)
def isolate_output_env(monkeypatch):
    monkeypatch.delenv("META_AGENT__AGENT_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("META_AGENT__PLANNER_MAX_TOKENS", raising=False)


def test_env_output_limit_overrides_planner_without_changing_it(monkeypatch):
    monkeypatch.setenv("META_AGENT__PLANNER_MAX_TOKENS", "1024")
    monkeypatch.setenv("META_AGENT__AGENT_MAX_OUTPUT_TOKENS", "4096")
    settings = AppTestSettings()
    assert settings.effective_agent_max_output_tokens == 4096
    assert settings.planner_max_tokens == 1024
    monkeypatch.delenv("META_AGENT__AGENT_MAX_OUTPUT_TOKENS")
    assert AppTestSettings().effective_agent_max_output_tokens == 1024
    monkeypatch.delenv("META_AGENT__PLANNER_MAX_TOKENS")
    assert AppTestSettings().effective_agent_max_output_tokens == 2048


@pytest.mark.parametrize("value", ["0", "127", "8193", "-1", "bad", ""])
def test_invalid_output_limit_rejected_at_settings_load(monkeypatch, value):
    monkeypatch.setenv("META_AGENT__AGENT_MAX_OUTPUT_TOKENS", value)
    with pytest.raises(ValidationError):
        AppTestSettings()


@pytest.mark.parametrize(
    "configured,effective", [(None, 1536), (128, 128), (4096, 4096), (8192, 8192)]
)
def test_agent_model_receives_effective_limit_and_metrics(monkeypatch, configured, effective):
    captured = []

    def init_model(*args, **kwargs):
        captured.append(kwargs)
        return BoundFakeModel(messages=iter([AIMessage("你好，我可以协助查询记录。")]))

    monkeypatch.setattr("meta_agent.llm.models.init_chat_model", init_model)

    async def check():
        async with runtime(
            orchestration_mode="agent",
            deepseek_api_key="synthetic-key",
            planner_max_tokens=1536,
            agent_max_output_tokens=configured,
        ) as (container, _):
            run = await container.application.execute(request("你好"))
            assert run.record.status == "succeeded"
            assert captured[0]["max_tokens"] == effective
            assert container.settings.planner_max_tokens == 1536
            assert run.record.metrics["agent_max_output_tokens"] == effective

    asyncio.run(check())


def test_legacy_planner_model_keeps_its_own_limit(monkeypatch):
    captured = {}

    def init_model(*args, **kwargs):
        captured.update(kwargs)
        return BoundFakeModel(messages=iter([]))

    monkeypatch.setattr("meta_agent.llm.models.init_chat_model", init_model)
    create_planner_model(
        AppTestSettings(
            deepseek_api_key="synthetic-key",
            planner_max_tokens=1024,
            agent_max_output_tokens=4096,
        )
    )
    assert captured["max_tokens"] == 1024


def test_context_guard_reserves_agent_output_instead_of_planner_output(monkeypatch):
    monkeypatch.setattr("meta_agent.agent.runtime.estimate_tokens", lambda _: 5000)
    monkeypatch.setattr(
        "meta_agent.llm.models.init_chat_model",
        lambda *args, **kwargs: BoundFakeModel(messages=iter([AIMessage("不应调用模型。")])),
    )

    async def check():
        async with runtime(
            orchestration_mode="agent",
            deepseek_api_key="synthetic-key",
            planner_max_tokens=1024,
            agent_max_output_tokens=4096,
            model_context_tokens=8192,
        ) as (container, _):
            run = await container.application.execute(request("你好"))
            assert run.record.status == "unavailable"
            assert not run.record.metrics.get("agent_model_steps")
            assert any(
                e.type == "task_failed" and e.payload["code"] == "agent_context_overflow"
                for e in run.record.events
            )

    asyncio.run(check())


@pytest.mark.parametrize("reason,expected", [("stop", "succeeded"), ("length", "partial")])
def test_truncated_answer_is_preserved_but_never_reported_successful(monkeypatch, reason, expected):
    monkeypatch.setattr(
        "meta_agent.llm.models.init_chat_model",
        lambda *args, **kwargs: BoundFakeModel(
            messages=iter(
                [
                    AIMessage(
                        "已生成的回答内容。",
                        response_metadata={"finish_reason": reason},
                        usage_metadata={"input_tokens": 20, "output_tokens": 8, "total_tokens": 28},
                    )
                ]
            )
        ),
    )

    async def check():
        async with runtime(
            orchestration_mode="agent",
            deepseek_api_key="synthetic-key",
            agent_max_output_tokens=4096,
        ) as (container, _):
            run = await container.application.execute(request("你好"))
            assert answers(run.record) == "已生成的回答内容。"
            assert run.record.status == expected
            assert run.record.goal_statuses["agent"] == expected
            assert run.record.events[-1].type == "completed"
            assert run.record.events[-1].payload["outcome"] == expected
            assert run.record.metrics["agent_answer_finish_reason"] == reason
            assert run.record.metrics["agent_answer_truncated"] == (reason == "length")
            assert run.record.metrics["agent_model_steps"][-1]["finish_reason"] == reason
            notices = [e.payload["text"] for e in run.record.events if e.type == "progress"]
            assert any("输出长度限制" in text for text in notices) == (reason == "length")

    asyncio.run(check())
