"""
创建日期：2026-10-07
文件功能：验证IReTour历史筛选、可信身份、多轮记录定位及报表停用。
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from meta_agent.agent.schemas import GoHistoryArgs, TourHistoryArgs
from meta_agent.agent.tools import available_tools, build_tools
from meta_agent.contracts import DomainError
from meta_agent.tools.ai_webapi import IRETOUR_REPORT_ENDPOINTS, AIWebApiClient
from tests.helpers.runtime import answers, request, runtime
from tests.helpers.settings import AppTestSettings
from tests.test_single_agent import SCOPE, activate, tool_call


def test_tour_history_uses_its_own_filter_contract():
    args = TourHistoryArgs(
        record_scope="with_result", training_state="completed", result_state="summary_available"
    )
    assert args.model_dump()["record_scope"] == "with_result"
    assert TourHistoryArgs(record_scope="without_result").record_scope == "without_result"
    assert TourHistoryArgs(activity_scope="all").activity_scope == "all"
    assert GoHistoryArgs(record_scope="past").record_scope == "past"
    with pytest.raises(ValidationError):
        GoHistoryArgs(record_scope="with_result")


@pytest.mark.parametrize(
    "kwargs", [{"record_scope": "past"}, {"training_state": "unknown"}, {"result_state": "usable"}]
)
def test_tour_history_rejects_filters_not_in_backend_contract(kwargs):
    with pytest.raises(ValidationError):
        TourHistoryArgs(**kwargs)


def test_tour_history_paging_and_selected_session_keep_identity_and_evidence():
    async def check():
        async with runtime() as (c, backend):
            activate(
                c,
                [
                    tool_call("get_iretour_patient_history", {"record_scope": "with_result"}),
                    AIMessage("已读取有结果的训练历史。"),
                ],
            )
            first = await c.application.execute(request("查看IReTour有结果的训练", scope=SCOPE))
            task = next(
                r
                for r in first.record.results.values()
                if r.outputs.get("tool_name") == "get_iretour_patient_history"
            )
            evidence = await c.repository.evidence(SCOPE.scope_hash, task.evidence_ids[0])
            ref = evidence.payload["data"]["items"][0]["session_ref"]
            activate(
                c,
                [
                    tool_call(
                        "get_iretour_patient_history",
                        {"page_number": 2, "record_scope": "with_result"},
                    ),
                    AIMessage("第二页没有更多记录。"),
                ],
            )
            await c.application.execute(request("下一页", "r2", scope=SCOPE))
            activate(
                c,
                [
                    tool_call(
                        "get_iretour_session_analysis",
                        {"selector": "session_ref", "session_ref": ref},
                    ),
                    AIMessage("已解读刚才的训练。"),
                ],
            )
            third = await c.application.execute(request("解读第一页第一条训练", "r3", scope=SCOPE))
            calls = [(name, body) for name, body in backend.calls if "iretour" in name]
            assert [name for name, _ in calls] == [
                "get_iretour_patient_history",
                "get_iretour_patient_history",
                "get_iretour_session_analysis",
            ]
            assert calls[1][1]["page_number"] == 2
            assert calls[2][1]["session_ref"] == ref
            assert all(body["system_context"]["user"] == "3799" for _, body in calls)
            assert not any(name == "resolve_patient_identity" for name, _ in backend.calls)
            memory = await c.repository.conversation(SCOPE.thread_id("c1"))
            assert ref in json.dumps(memory.agent_messages)
            assert third.record.status == "succeeded"

    asyncio.run(check())


def test_disabled_tour_reports_are_hidden_but_readers_and_go_reports_remain():
    ctx = SimpleNamespace(scope=SCOPE, settings=AppTestSettings())
    names = {tool.name for tool in available_tools(ctx, build_tools())}
    assert not names & IRETOUR_REPORT_ENDPOINTS
    assert {
        "get_iretour_patient_history",
        "get_iretour_session_analysis",
        "get_iretour_longitudinal_analysis",
        "generate_irego_single_session_report",
        "generate_irego_longitudinal_report",
    } <= names
    ctx.settings.iretour_reports_enabled = True
    assert {t.name for t in available_tools(ctx, build_tools())} >= IRETOUR_REPORT_ENDPOINTS


@pytest.mark.parametrize("endpoint", sorted(IRETOUR_REPORT_ENDPOINTS))
def test_agent_cannot_execute_disabled_tour_report_even_if_model_requests_it(endpoint):
    async def check():
        async with runtime() as (c, backend):
            args = {"session_ref": "synthetic-tour-1"} if "single_session" in endpoint else {}
            activate(c, [tool_call(endpoint, args), AIMessage("IReTour报表暂未启用。")])
            run = await c.application.execute(request("生成IReTour报表", scope=SCOPE))
            assert endpoint not in [name for name, _ in backend.calls]
            assert any(r.code == "tool_not_allowed" for r in run.record.results.values())
            assert not any(e.type == "artifact_ready" for e in run.record.events)

    asyncio.run(check())


@pytest.mark.parametrize("endpoint", sorted(IRETOUR_REPORT_ENDPOINTS))
def test_disabled_tour_report_never_reaches_http(endpoint):
    async def check():
        calls = []
        client = AIWebApiClient(
            AppTestSettings(dry_run=False),
            transport=httpx.MockTransport(
                lambda req: calls.append(req) or httpx.Response(200, json={})
            ),
        )
        try:
            with pytest.raises(DomainError) as caught:
                await client.post(endpoint, {})
            assert caught.value.code == "capability_disabled"
            assert caught.value.outcome == "unsupported"
            assert not calls
        finally:
            await client.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "endpoint,ref",
    [
        ("generate_irego_single_session_report", "iretour_s_v1_fixture"),
        ("get_iretour_session_analysis", "irego_s_v1_fixture"),
    ],
)
def test_cross_device_ref_is_rejected_before_identity_mapping_or_backend_call(endpoint, ref):
    async def check():
        async with runtime() as (c, backend):
            args = {"session_ref": ref}
            if "analysis" in endpoint:
                args["selector"] = "session_ref"
            activate(c, [tool_call(endpoint, args), AIMessage("这类记录不能使用另一设备的工具。")])
            run = await c.application.execute(request("处理这条训练记录", scope=SCOPE))
            assert any(r.code == "record_domain_mismatch" for r in run.record.results.values())
            assert not any(
                name in {endpoint, "resolve_patient_identity"} for name, _ in backend.calls
            )
            assert not any(e.type == "artifact_ready" for e in run.record.events)

    asyncio.run(check())


def test_legacy_report_disable_keeps_analysis_and_does_not_call_backend_report():
    async def check():
        async with runtime() as (c, backend):
            run = await c.application.execute(
                request("解读IReTour最近训练并生成报告图片", scope=SCOPE)
            )
            assert run.record.status == "partial"
            assert "get_iretour_session_analysis" in [name for name, _ in backend.calls]
            assert not IRETOUR_REPORT_ENDPOINTS & {name for name, _ in backend.calls}
            assert "速度" in answers(run.record)
            assert not any(e.type == "artifact_ready" for e in run.record.events)

    asyncio.run(check())
