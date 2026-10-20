"""
创建日期：2026-09-24
文件功能：测试患者档案解析（身份识别装填）与意图解析器上下文注入。
"""

import asyncio
from typing import Any

from meta_agent.context.budget import LLMBudget
from meta_agent.contracts import ConversationState, IntentDecision
from meta_agent.domains.patient_identity import brief_from_context_response
from meta_agent.planning.parser import ConservativePlanner, StructuredIntentPlanner
from tests.helpers.settings import AppTestSettings


def test_brief_full_facts_render_and_name_rule_present() -> None:
    body = {
        "status": "success",
        "data": {
            "identity_binding": {"binding_status": "verified"},
            "profile_brief": {
                "available": True,
                "facts": {
                    "sex": "女",
                    "age": 65,
                    "height": 1.62,
                    "weight": 58.0,
                    "diagnosis": "脑卒中",
                },
            },
        },
    }

    brief = brief_from_context_response(body)

    assert brief.is_patient
    assert brief.facts["age"] == 65
    assert "性别：女" in brief.display_text
    assert "年龄：65" in brief.display_text
    assert "诊断：脑卒中" in brief.display_text
    assert "不得提及患者姓名" in brief.display_text


def test_brief_name_gated_by_include_name_and_phone_excluded() -> None:
    """姓名默认不装填（联调关闭状态）；开启后保留且受提示词约束；手机号恒不装填。"""
    body = {
        "data": {
            "profile_brief": {
                "available": True,
                "facts": {"name": "张三", "phone": "13800000000", "age": 60},
            }
        }
    }

    default_off = brief_from_context_response(body)
    assert default_off.is_patient
    assert "张三" not in default_off.display_text
    assert "13800000000" not in default_off.display_text

    enabled = brief_from_context_response(body, include_name=True)
    assert "姓名：张三" in enabled.display_text
    assert "13800000000" not in enabled.display_text


def test_brief_empty_or_malformed_degrades_to_not_patient() -> None:
    assert not brief_from_context_response(
        {"data": {"profile_brief": {"available": False, "facts": {}}}}
    ).is_patient
    assert not brief_from_context_response({"data": {}}).is_patient
    assert not brief_from_context_response({"data": "broken"}).is_patient
    assert not brief_from_context_response(None).is_patient
    assert not brief_from_context_response("not-a-dict").is_patient


def test_brief_verified_binding_without_facts_still_patient() -> None:
    body = {"data": {"identity_binding": {"binding_status": "verified"}}}

    brief = brief_from_context_response(body)

    assert brief.is_patient
    assert "不得提及患者姓名" in brief.display_text


class CapturingPlannerModel:
    """记录输入消息并返回预设决策的测试模型。"""

    def __init__(self, decision: IntentDecision) -> None:
        self._decision = decision
        self.messages: list[Any] = []

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        return self

    async def ainvoke(self, messages: Any) -> Any:
        self.messages = messages
        return self._decision.model_dump(mode="json")


def test_structured_planner_injects_patient_brief_into_context() -> None:
    settings = AppTestSettings(
        app_env="test",
        dry_run=True,
        persistence_backend="memory",
        planner_mode="deterministic",
    )
    decision = IntentDecision(decision="respond", decision_summary="你好")
    model = CapturingPlannerModel(decision)
    planner = StructuredIntentPlanner(model, settings)

    result = asyncio.run(
        planner.parse(
            "你好",
            ConversationState(),
            LLMBudget(settings.max_llm_calls),
            patient_brief="当前患者档案（内部参考）：\n- 年龄：65",
        )
    )

    assert result.decision == "respond"
    human = [m for m in model.messages if m[0] == "human"][0][1]
    assert "patient_brief" in human
    assert "年龄：65" in human


def test_conservative_planner_accepts_patient_brief_kwarg() -> None:
    settings = AppTestSettings(
        app_env="test",
        dry_run=True,
        persistence_backend="memory",
        planner_mode="deterministic",
    )
    planner = ConservativePlanner()

    result = asyncio.run(
        planner.parse(
            "你好",
            ConversationState(),
            LLMBudget(settings.max_llm_calls),
            patient_brief="当前患者档案（内部参考）：\n- 年龄：65",
        )
    )

    assert result.decision == "respond"
