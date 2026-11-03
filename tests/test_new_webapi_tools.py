"""
创建日期：2026-10-02
文件功能：验证新工具路由、独立契约、事实来源、多源停用及跨轮设备和患者隔离。
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from meta_agent.contracts import DomainError, Goal, IReTourRequest, Selector
from meta_agent.domains.hospital import (
    HospitalAdapter,
    _resolve_window,
    hospital_report_urls,
    render_hospital_markdown,
)
from meta_agent.orchestration.identity import TrustedScope, trusted_scope_from_inputs
from meta_agent.tools.ai_webapi import (
    ASSESSMENT_REPORT_ENDPOINTS,
    IRECOG_REPORT_ENDPOINTS,
    IRETOUR_REPORT_ENDPOINTS,
    PATIENT_ENDPOINTS,
    AIWebApiClient,
)
from meta_agent.tools.demo import demo_response
from tests.helpers.runtime import SeedPlanner, answers, runtime
from tests.helpers.runtime import request as base_request
from tests.helpers.settings import AppTestSettings


def request(query, rid="r1", **kwargs):
    kwargs.setdefault("scope", TrustedScope("tenant", "actor", "461", iretour_patient_id="3799"))
    return base_request(query, rid, **kwargs)


def tour_report(payload):
    return {
        "tool_name": "generate_iretour_single_session_report",
        "contract_version": "iretour-1.0.0",
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
            assert [name for name, _ in backend.calls] == ["generate_iretour_single_session_report"]
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


def test_tour_identity_namespace_is_separate():
    async def check():
        async with runtime() as (c, backend):
            scope = TrustedScope("t", "a", "3892", iretour_patient_id="3799")
            await c.application.execute(request("查看IReTour训练历史", scope=scope))
            assert backend.calls[-1][1]["system_context"]["user"] == "3799"
            await c.application.execute(request("查看IReGo训练历史", "r2", scope=scope))
            assert backend.calls[-1][1]["system_context"]["user"] == "3892"
        a = trusted_scope_from_inputs({"iretourPatientId": "3799"}, "a", "t")
        b = trusted_scope_from_inputs({"iretourPatientId": "3780"}, "a", "t")
        assert a.scope_hash != b.scope_hash

    asyncio.run(check())


def test_scope_resolves_spaceid_lowercase():
    scope = trusted_scope_from_inputs(
        {"spaceid": "67ecd511-5532-405d-9b70-1cd1c0532eed"}, "a", "t"
    )
    assert scope.space_id == "67ecd511-5532-405d-9b70-1cd1c0532eed"


def test_tour_does_not_treat_robot_identity_as_project_identity():
    async def check():
        async with runtime() as (c, backend):
            scope = TrustedScope("tenant", "actor", "3799")
            run = await c.application.execute(request("查看IReTour训练历史", scope=scope))
            assert run.record.status == "clarification"
            assert not backend.calls

    asyncio.run(check())


@pytest.mark.parametrize(
    "legacy,registry,expected",
    [(False, None, "succeeded"), (True, "iretour-1.0.0", "succeeded"), (True, "1.6.0", "failed")],
)
def test_tour_contract_compatibility_is_explicit(legacy, registry, expected):
    async def check():
        async with runtime() as (c, backend):
            body = demo_response("get_iretour_patient_context", {})
            if legacy:
                body["contract_version"] = "1.6.0"
                body["meta"]["registry_versions"] = {"contract": registry}
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


def test_identity_mapping_rejects_mismatch_and_routes_only_trusted_inputs():
    async def check():
        calls = []

        def respond(req):
            calls.append(req)
            return httpx.Response(
                200,
                json={
                    "tool_name": "resolve_patient_identity",
                    "contract_version": "1.6.0",
                    "status": "success",
                    "data": {
                        "project_patient_id": 3799,
                        "robot_patient_id": 461,
                        "binding_status": "resolved",
                    },
                },
            )

        client = AIWebApiClient(
            AppTestSettings(dry_run=False), transport=httpx.MockTransport(respond)
        )
        try:
            assert (await client.resolve_patient_identity(project_patient_id=3799))[
                "robot_patient_id"
            ] == 461
            with pytest.raises(DomainError):
                await client.resolve_patient_identity(project_patient_id=1)
            with pytest.raises(DomainError):
                await client.resolve_patient_identity(project_patient_id=3799, phone="123")
            assert len(calls) == 2
        finally:
            await client.close()

    asyncio.run(check())


def test_resolve_window_relative_and_range():
    # Asia/Shanghai 语义：本月=1号至今，上月=整月，近N天=含今天往回数。
    assert _resolve_window(None, None, None, "2026-10-20") == ("2026-10-01", "2026-10-20")
    assert _resolve_window("this_month", None, None, "2026-10-20") == ("2026-10-01", "2026-10-20")
    assert _resolve_window("last_month", None, None, "2026-10-20") == ("2026-09-01", "2026-09-30")
    assert _resolve_window("last_7_days", None, None, "2026-10-20") == ("2026-10-14", "2026-10-20")
    assert _resolve_window("last_30_days", None, None, "2026-10-20") == ("2026-09-21", "2026-10-20")
    assert _resolve_window("today", None, None, "2026-10-20") == ("2026-10-20", "2026-10-20")
    assert _resolve_window("yesterday", None, None, "2026-10-20") == ("2026-10-19", "2026-10-19")
    assert _resolve_window("range", "2026-09-01", "2026-09-14", "2026-10-20") == (
        "2026-09-01",
        "2026-09-14",
    )


def _hospital_ctx(entries):
    class FakeBackend:
        def __init__(self, entries):
            self.entries = entries

        async def directory(self):
            return self.entries

    return SimpleNamespace(backend=FakeBackend(entries))


def test_hospital_rejects_hospital_id_outside_directory():
    async def check():
        adapter = HospitalAdapter()
        ctx = _hospital_ctx([{"hospital_id": 1, "hospital_name": "甲"}])
        with pytest.raises(DomainError) as exc:
            await adapter.execute(
                SimpleNamespace(task_id="t1"),
                {"scope": "institution", "hospital_id": 999},
                ctx,
            )
        assert exc.value.code == "hospital_required"

    asyncio.run(check())


def test_hospital_directory_filters_by_name():
    async def check():
        adapter = HospitalAdapter()
        ctx = _hospital_ctx(
            [
                {"hospital_id": 86, "hospital_name": "上海市静安区彭浦镇第二社区卫生服务中心"},
                {"hospital_id": 87, "hospital_name": "上海市第二康复医院"},
            ]
        )
        matched = await adapter.directory(SimpleNamespace(task_id="t1"), {"name": "康复"}, ctx)
        assert matched.status == "succeeded"
        assert [e["hospital_id"] for e in matched.outputs["directory"]] == [87]
        assert matched.outputs["total"] == 1

        miss = await adapter.directory(SimpleNamespace(task_id="t1"), {"name": "岳阳"}, ctx)
        assert miss.outputs["total"] == 0
        assert miss.outputs["directory"] == []

        full = await adapter.directory(SimpleNamespace(task_id="t1"), {}, ctx)
        assert full.outputs["total"] == 2

    asyncio.run(check())


def test_hospital_directory_capability_is_registered():
    # 根因回归：_adapter 用 capability="hospital.directory" 构造 TaskSpec，
    # 若未加入 Capability 枚举会抛 ValidationError 并吞成 tool_internal_error。
    from meta_agent.contracts import TaskSpec

    spec = TaskSpec(
        task_id="t1",
        goal_ids=["agent"],
        capability="hospital.directory",
        arguments={},
        idempotency_key="k",
    )
    assert spec.capability == "hospital.directory"


def test_hospital_snapshot_section_rejects_history_window():
    async def check():
        adapter = HospitalAdapter()
        ctx = _hospital_ctx([{"hospital_id": 1, "hospital_name": "甲"}])
        with pytest.raises(DomainError) as exc:
            await adapter.execute(
                SimpleNamespace(task_id="t1"),
                {
                    "scope": "institution",
                    "hospital_id": 1,
                    "section": "overview",
                    "date_mode": "last_month",
                },
                ctx,
            )
        assert exc.value.code == "invalid_request"

    asyncio.run(check())


def _hospital_response(section="all", scope="institution"):
    return {
        "tool_name": "query_hospital_operations",
        "contract_version": "1.2.0",
        "status": "success",
        "data": {
            "scope": scope,
            "section": section,
            "period": {"start_date": "2026-10-01", "end_date": "2026-10-20"},
            "hospitals": [
                {
                    "hospital": {
                        "hospital_id": 106,
                        "hospital_name": "上海中医药大学附属岳阳医院",
                        "tenant_level": 0,
                    },
                    "overview": {
                        "patient_count": 74,
                        "doctor_count": 48,
                        "daily_active_count": 0,
                        "platform_hospital_count": 2,
                        "platform_school_count": 6,
                    },
                    "consultation": {"consultation_people": 0, "consultation_times": 0, "time_scope": "query_window"},
                    "rooms": {"consultation_rooms": 0, "assessment_rooms": 0, "training_rooms": 0, "total_rooms": 0},
                    "assessment_training": {
                        "assessment_count": 0,
                        "scale_assessment_count": 0,
                        "paradigm_assessment_count": 0,
                        "offline_training_count": 0,
                        "device_training_count": 0,
                        "corridor_training_count": 0,
                        "time_scope": "upstream_snapshot",
                    },
                    "records": {
                        "real_consultation_count": 1,
                        "ai_consultation_count": 0,
                        "remote_consultation_count": 0,
                        "medical_record_count": 251,
                        "prescription_template_count": 114,
                        "report_count": 114,
                        "time_scope": "upstream_snapshot",
                    },
                }
            ],
            "comparison": None,
            "report": None,
        },
    }


def test_hospital_render_full_markdown():
    text = render_hospital_markdown(_hospital_response())
    assert text is not None
    assert "**上海中医药大学附属岳阳医院**" in text
    assert "查询内容：综合运营情况" in text
    assert "问诊统计区间：2026-10-01 至 2026-10-20" in text
    assert "| 注册患者 | 74 人 |" in text
    assert "| 医生 | 48 人 |" in text
    assert "| 真人问诊 | 1 次 |" in text
    assert "| 就诊记录 | 251 条 |" in text
    assert "| 报告 | 114 份 |" in text
    assert "简要说明" in text
    assert "可继续输入：换机构" in text
    # 平台口径字段与 derived_metrics 不进表格
    assert "platform_hospital_count" not in text
    assert "患者活跃率" not in text


def test_hospital_render_single_section_only():
    text = render_hospital_markdown(_hospital_response(section="overview"))
    assert "| 注册患者 | 74 人 |" in text
    assert "| 医生 | 48 人 |" in text
    assert "区间问诊人数" not in text
    assert "就诊记录" not in text


def test_hospital_render_returns_none_on_failure():
    assert render_hospital_markdown({"status": "failed", "data": None}) is None
    assert render_hospital_markdown({"status": "success", "data": {"hospitals": []}}) is None
    assert render_hospital_markdown("not a dict") is None


def test_hospital_report_urls_extracts_valid_artifact():
    response = _hospital_response()
    response["data"]["report"] = {
        "artifact_ref": "hospital_report_v2_abc",
        "image_url": "http://192.168.8.160:5043/api/ai/hospital-operations/report-artifacts/abc/content?format=png",
        "html_url": "http://192.168.8.160:5043/api/ai/hospital-operations/report-artifacts/abc/content?format=html",
        "expires_at": None,
    }
    image_url = hospital_report_urls(response)
    assert image_url.endswith("?format=png")
    # 表格本身不再内嵌 markdown 图片/链接；报表图片走 [Cweb]、[web] 是对话记录页。
    text = render_hospital_markdown(response)
    assert "![医院运营分析报告]" not in text
    assert "[查看完整报告]" not in text
    # 已生成报表时不再提示“生成报告”
    assert "生成报告" not in text


def test_hospital_report_urls_rejects_non_artifact_or_missing():
    response = _hospital_response()
    assert hospital_report_urls(response) is None  # 无 report
    response["data"]["report"] = {"image_url": "javascript:alert(1)", "html_url": "https://evil.example/x"}
    assert hospital_report_urls(response) is None  # 非法 scheme
    response["data"]["report"] = {
        "image_url": "http://192.168.8.160:5043/other/path?format=png",
        "html_url": "http://192.168.8.160:5043/other/path?format=html",
    }
    assert hospital_report_urls(response) is None  # 非报表制品路径
