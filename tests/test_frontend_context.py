"""
创建日期：2026-10-10
文件功能：验证UI记录归属、患者切换隔离、纯文字回答及Agent子任务失败事件。
"""

import asyncio

import pytest
from langchain_core.messages import AIMessage

from meta_agent.application.service import ApplicationRequest
from meta_agent.contracts import DomainError
from meta_agent.orchestration.identity import TrustedScope
from meta_agent.tools.demo import demo_response
from tests.helpers.runtime import runtime, scene_response
from tests.test_single_agent import SCOPE, activate, tool_call


def frontend_request(query, rid="ui-1", *, scope=SCOPE, context=None):
    return ApplicationRequest(
        query,
        scope,
        "same-conversation",
        rid,
        context=context or {"current_view": "home"},
        owner_key=scope.principal_hash,
    )


def test_greeting_with_patient_context_does_not_query_patient():
    async def check():
        async with runtime() as (c, backend):
            activate(c, [AIMessage("您好，有什么需要帮助的吗？")])
            run = await c.application.execute(frontend_request("你好"))
            assert not backend.calls
            assert [e.type for e in run.record.events] == ["accepted", "answer_part", "completed"]
            assert run.record.metrics["agent_tool_sets"][0]["names"] == []

    asyncio.run(check())


def test_selected_record_is_verified_before_model_and_patient_switch_drops_anchor():
    async def check():
        async with runtime() as (c, backend):
            selected = "iretour_s_v1_selected"

            def history(payload):
                body = demo_response("get_iretour_patient_history", payload)
                body["data"]["items"][0]["session_ref"] = selected
                return body

            backend.overrides["get_iretour_patient_history"] = history
            activate(
                c,
                [
                    tool_call(
                        "get_iretour_session_analysis",
                        {"selector": "session_ref", "session_ref": selected},
                    ),
                    AIMessage("您选择的这次训练已完成。"),
                ],
            )
            run = await c.application.execute(
                frontend_request(
                    "解释一下这次训练",
                    context={"selected_session_ref": selected, "current_view": "details"},
                )
            )
            assert run.record.status == "succeeded"
            assert backend.calls[0][0] == "get_iretour_patient_history"
            assert all(
                p["system_context"]["user"] == "3799"
                for name, p in backend.calls
                if "system_context" in p
            )
            assert run.context.memory.current_record.session_ref == selected
            assert run.context.frontend_context["selected_record_domain"] == "iretour"
            assert any(
                u["prompt_id"] == "frontend.context" for u in run.record.metrics["prompt_usages"]
            )
            other = TrustedScope(
                "tenant", "actor", project_patient_id="3780", iretour_patient_id="3780"
            )
            activate(c, [AIMessage("请先明确当前训练记录。")])
            second = await c.application.execute(
                frontend_request("上一次呢？", "ui-2", scope=other)
            )
            assert (
                second.record.status == "clarification" and second.record.metrics["llm_calls"] == 0
            )
            assert second.context.memory.current_record is None
            assert not (
                await c.repository.conversation(other.thread_id("same-conversation"))
            ).current_record

    asyncio.run(check())


def test_unknown_selected_record_clarifies_without_model_or_fallback():
    async def check():
        async with runtime() as (c, backend):
            activate(c, [AIMessage("不应进入模型。")])
            run = await c.application.execute(
                frontend_request(
                    "解释一下这次训练",
                    context={"selected_session_ref": "iretour_s_v1_other_patient"},
                )
            )
            assert run.record.status == "clarification"
            assert [n for n, _ in backend.calls] == ["get_iretour_patient_history"]
            assert not run.context.memory.current_record
            assert not any(e.type == "answer_part" for e in run.record.events)
            assert run.record.events[-2].type == "clarification"
            assert run.record.metrics["llm_calls"] == 0

    asyncio.run(check())


@pytest.mark.parametrize(
    "unsafe",
    [
        "报告见 https://images.example.test/report.png",
        "[answer]回答",
        "[ScenePoint:]105",
        '{"tool_name":"get_patient_profile","data":{}}',
    ],
)
def test_model_control_and_media_output_cannot_reach_tts_answer(unsafe):
    async def check():
        async with runtime() as (c, _):
            activate(c, [AIMessage(unsafe)])
            run = await c.application.execute(frontend_request("回答我的问题"))
            assert not any(e.type == "answer_part" for e in run.record.events)
            assert run.record.status == "failed"
            assert any(
                e.type == "task_failed" and e.payload["code"] == "answer_not_displayable"
                for e in run.record.events
            )

    asyncio.run(check())


def test_tool_failure_event_and_useful_answer_produce_partial():
    async def check():
        async with runtime() as (c, backend):
            backend.overrides["generate_irego_single_session_report"] = DomainError(
                "artifact_unavailable", "报告暂不可用。"
            )
            activate(
                c,
                [
                    tool_call("get_irego_session_analysis"),
                    tool_call(
                        "generate_irego_single_session_report",
                        {"session_ref": "synthetic-session"},
                        "report-call",
                    ),
                    AIMessage("训练已完成，报告暂时无法生成。"),
                ],
            )
            scope = TrustedScope(
                "tenant", "actor", "461", project_patient_id="3799", iretour_patient_id="3799"
            )
            run = await c.application.execute(
                frontend_request("解释最近一次训练并生成报告", scope=scope)
            )
            assert run.record.status == "partial"
            assert any(e.type == "answer_part" for e in run.record.events)
            assert any(
                e.type == "task_failed" and e.payload["code"] == "artifact_unavailable"
                for e in run.record.events
            )
            assert not any(e.type == "artifact_ready" for e in run.record.events)

    asyncio.run(check())


def test_multiple_actions_from_same_agent_tool_have_distinct_ids_and_order():
    async def check():
        async with runtime(scene_actions_enabled=True) as (c, backend):
            backend.overrides["navigate_scene"] = scene_response
            scope = TrustedScope("tenant", "actor", space_id="space", scene_version=7)
            query = "打开历史，打开个人中心，打开帮助"
            activate(
                c,
                [
                    tool_call("navigate_scene", {"target": query}),
                    AIMessage("已发送这三个打开请求。"),
                ],
            )
            run = await c.application.execute(frontend_request(query, scope=scope))
            actions = [e for e in run.record.events if e.type == "action_ready"]
            assert len(actions) == 3 and len({e.payload["action_id"] for e in actions}) == 3
            assert [e.payload["command_order"] for e in actions] == [1, 2, 3]

    asyncio.run(check())
