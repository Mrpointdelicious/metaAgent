"""
创建日期：2026-10-02
文件功能：验证新工具路由、独立契约、事实来源、多源停用及跨轮设备和患者隔离。
"""

import asyncio

import httpx
import pytest

from meta_agent.contracts import DomainError, Goal, IReTourRequest, Selector
from meta_agent.orchestration.identity import TrustedScope, trusted_scope_from_inputs
from meta_agent.tools.ai_webapi import (
    ASSESSMENT_REPORT_ENDPOINTS,
    IRECOG_REPORT_ENDPOINTS,
    IRETOUR_REPORT_ENDPOINTS,
    PATIENT_ENDPOINTS,
    AIWebApiClient,
)
from meta_agent.tools.demo import demo_response
from tests.helpers.identity import patient_scope
from tests.helpers.runtime import SeedPlanner, answers, runtime
from tests.helpers.runtime import request as base_request
from tests.helpers.settings import AppTestSettings


def request(query, rid="r1", **kwargs):
    kwargs.setdefault("scope", patient_scope("tenant", "actor", "3799"))
    return base_request(query, rid, **kwargs)


def tour_report(payload):
    return {
        "tool_name": "generate_iretour_single_session_report",
        "contract_version": "2.0.0",
        "request_id": "fixture",
        "status": "success",
        "meta": {},
        "data": {"artifact_ref": "tour-image", "image_url": "https://images.example.test/tour.png"},
    }


@pytest.mark.parametrize(
    "endpoint", sorted(PATIENT_ENDPOINTS - {"get_multisource_patient_context"})
)
def test_patient_endpoint_uses_patient_base(endpoint):
    async def check():
        calls = []

        def respond(req):
            calls.append(req.url.path)
            return httpx.Response(200, json={})

        client = AIWebApiClient(
            AppTestSettings(
                dry_run=False,
                iretour_reports_enabled=endpoint in IRETOUR_REPORT_ENDPOINTS,
                irecog_reports_enabled=endpoint in IRECOG_REPORT_ENDPOINTS,
                assessment_reports_enabled=endpoint in ASSESSMENT_REPORT_ENDPOINTS,
            ),
            transport=httpx.MockTransport(respond),
        )
        try:
            await client.post(endpoint, {})
            assert calls == [f"/api/ai/patients/tools/{endpoint}"]
        finally:
            await client.close()

    asyncio.run(check())


def test_disabled_multisource_never_reaches_network_or_enrichment():
    async def check():
        client = AIWebApiClient(AppTestSettings(dry_run=True))
        try:
            with pytest.raises(DomainError, match="暂未启用"):
                await client.post("get_multisource_patient_context", {})
        finally:
            await client.close()
        async with runtime() as (c, backend):
            for i, query in enumerate(["你好", "查看患者概况", "查看训练历史", "查看IReTour概况"]):
                await c.application.execute(request(query, str(i)))
            assert all(
                endpoint != "get_multisource_patient_context" for endpoint, _ in backend.calls
            )

    asyncio.run(check())


@pytest.mark.parametrize(
    "query,endpoint",
    [
        ("查看IReTour概况", "get_iretour_patient_context"),
        ("查看IReTour训练历史", "get_iretour_patient_history"),
        ("解读IReTour最近训练", "get_iretour_session_analysis"),
        ("解读IReTour最近4次训练趋势", "get_iretour_longitudinal_analysis"),
    ],
)
def test_tour_smoke_with_verified_fact_sources(query, endpoint):
    async def check():
        async with runtime() as (c, backend):
            run = await c.application.execute(request(query))
            assert run.record.status == "succeeded"
            assert backend.calls[-1][0] == endpoint
            assert run.record.results["t1"].facts
            assert not any(r.code == "fact_source_mismatch" for r in run.record.results.values())

    asyncio.run(check())


def test_tour_followups_keep_record_and_history_domain():
    async def check():
        async with runtime(iretour_reports_enabled=True) as (c, backend):
            backend.overrides["generate_iretour_single_session_report"] = tour_report
            await c.application.execute(request("解读IReTour最近训练"))
            backend.calls.clear()
            run = await c.application.execute(request("为这次训练生成报告图片", "r2"))
            assert run.record.status == "succeeded"
            assert [name for name, _ in backend.calls] == [
                "get_iretour_session_analysis",
                "generate_iretour_single_session_report",
            ]
            assert backend.calls[0][1]["session_ref"] == "synthetic-tour-1"
            await c.application.execute(request("那上一次呢", "r3"))
            assert backend.calls[-1][1]["session_ref"] == "synthetic-tour-2"
            await c.application.execute(request("查看IReTour训练历史", "r4"))
            await c.application.execute(request("下一页", "r5"))
            assert backend.calls[-1][0] == "get_iretour_patient_history"
            assert backend.calls[-1][1]["page_number"] == 2

    asyncio.run(check())


def test_cross_device_reference_is_not_sent_to_backend():
    async def check():
        async with runtime() as (c, backend):
            await c.application.execute(request("解读IReTour最近训练"))
            backend.calls.clear()
            run = await c.application.execute(request("解读IReGo这次训练", "r2"))
            assert run.record.status == "clarification"
            assert not backend.calls

    asyncio.run(check())


def test_all_patient_tools_share_one_typed_identity():
    async def check():
        async with runtime() as (c, backend):
            scope = patient_scope("t", "a", "3799")
            await c.application.execute(request("查看IReTour训练历史", scope=scope))
            await c.application.execute(request("查看IReGo训练历史", "r2", scope=scope))
            assert {p["system_context"]["user"] for _, p in backend.calls} == {"a"}
            assert all(
                p["system_context"]["patient_identity"]
                == {"type": "project_user_id", "value": "3799"}
                for _, p in backend.calls
            )
        a = trusted_scope_from_inputs(
            {
                "patient_identity": {"type": "login_name", "value": "000007"},
                "identity_context_token": "proof",
            },
            "a",
            "t",
        )
        assert a.patient_identity.value == "000007"

    asyncio.run(check())


@pytest.mark.parametrize(
    "version,expected", [("2.0.0", "succeeded"), ("1.6.0", "failed"), ("iretour-1.0.0", "failed")]
)
def test_patient_contract_is_v2_only(version, expected):
    async def check():
        async with runtime() as (c, backend):
            body = demo_response("get_iretour_patient_context", {})
            body["contract_version"] = version
            backend.overrides["get_iretour_patient_context"] = body
            run = await c.application.execute(request("查看IReTour概况"))
            assert run.record.status == expected

    asyncio.run(check())


@pytest.mark.parametrize(
    "count,artifact,expected",
    [
        (2, False, "succeeded"),
        (2, True, "clarification"),
        (3, True, "partial"),
        (20, False, "succeeded"),
        (21, False, "clarification"),
    ],
)
def test_tour_trend_window_limits(count, artifact, expected):
    async def check():
        async with runtime() as (c, backend):
            c.application.planner = SeedPlanner(
                [
                    Goal(
                        goal_id="g1",
                        kind="iretour",
                        domain="iretour",
                        query_span="训练趋势",
                        iretour=IReTourRequest(
                            operation="trend",
                            selector=Selector(mode="latest_count", count=count),
                            need_artifact=artifact,
                        ),
                    )
                ]
            )
            run = await c.application.execute(request("训练趋势"))
            assert run.record.status == expected
            if expected == "clarification":
                assert not backend.calls
            if expected == "partial":
                assert run.record.results["t1"].facts

    asyncio.run(check())


def test_tour_reference_mismatch_fails_without_report():
    async def check():
        async with runtime() as (c, backend):
            body = demo_response("get_iretour_session_analysis", {"session_ref": "wrong"})
            backend.overrides["get_iretour_session_analysis"] = body
            run = await c.application.execute(request("解读IReTour最近训练并生成报告图片"))
            assert run.record.status == "failed"
            assert not any(name.startswith("generate") for name, _ in backend.calls)

    asyncio.run(check())


def test_hospital_operator_query_and_patient_rejection():
    async def check():
        async with runtime() as (c, backend):
            query = "查询医院1的运营情况"
            rejected = await c.application.execute(request(query))
            assert rejected.record.status == "unsupported" and not backend.calls
            scope = TrustedScope("tenant", "operator", role="operator")
            run = await c.application.execute(request(query, "r2", scope=scope))
            assert run.record.status == "succeeded"
            assert "患者数：10" in answers(run.record)
            assert backend.calls[-1][0] == "query_hospital_operations"

    asyncio.run(check())


def test_identity_resolution_uses_the_signed_v2_context():
    async def check():
        import json

        calls = []

        def respond(req):
            payload = json.loads(req.content)
            calls.append(payload)
            body = demo_response("resolve_patient_identity", payload)
            return httpx.Response(200, json=body)

        client = AIWebApiClient(
            AppTestSettings(dry_run=False), transport=httpx.MockTransport(respond)
        )
        try:
            scope = patient_scope("t", "actor", "3799")
            resolved = await client.resolve_patient_identity(scope)
            assert resolved.has_patient
            assert calls[0]["system_context"]["user"] == "actor"
            assert calls[0]["system_context"]["patient_identity"]["value"] == "3799"
            assert "project_patient_id" not in calls[0]
            with pytest.raises(ValueError):
                trusted_scope_from_inputs({"projectPatientId": "3799"}, "actor", "t")
        finally:
            await client.close()

    asyncio.run(check())
