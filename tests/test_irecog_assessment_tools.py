"""
创建日期：2026-10-20
文件功能：验证 IReCog 与线上量表新工具的端点路由、契约版本、报表快照链及确定性意图解析。
"""

import asyncio

import httpx
import pytest
from pydantic import ValidationError

from meta_agent.agent.schemas import (
    AssessmentReportArgs,
    AssessmentSessionArgs,
    AssessmentTrendArgs,
    IReCogReportArgs,
    IReCogSessionArgs,
)
from meta_agent.agent.tools import build_tools
from meta_agent.contracts import DomainError
from meta_agent.orchestration.identity import TrustedScope
from meta_agent.tools.ai_webapi import (
    ASSESSMENT_REPORT_ENDPOINTS,
    IRECOG_REPORT_ENDPOINTS,
    AIWebApiClient,
)
from tests.helpers.runtime import answers, request, runtime
from tests.helpers.settings import AppTestSettings
from tests.test_single_agent import SCOPE

IRECOG_ENDPOINTS = {
    "get_irecog_patient_context",
    "get_irecog_patient_history",
    "get_irecog_session_analysis",
    "get_irecog_longitudinal_analysis",
    "generate_irecog_single_session_report",
    "generate_irecog_longitudinal_report",
}
ASSESSMENT_ENDPOINTS = {
    "get_assessment_patient_context",
    "get_assessment_patient_history",
    "get_assessment_result_analysis",
    "get_assessment_longitudinal_analysis",
    "generate_assessment_single_report",
    "generate_assessment_longitudinal_report",
}

SNAPSHOT = "a" * 64


def envelope(name, contract, data, status="success"):
    return {
        "tool_name": name,
        "contract_version": contract,
        "request_id": "fixture",
        "status": status,
        "meta": {},
        "patient_message": None,
        "reason_codes": [],
        "data": data,
    }


def artifact_envelope(name, contract):
    return envelope(
        name,
        contract,
        {
            "artifact_ref": f"{name}-artifact",
            "image_url": "https://images.example.test/report.png",
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    )


def irecog_session(ref="irecog_s_v1_abc"):
    return {
        "session_ref": ref,
        "recorded_at": "2026-09-01T10:00:00+08:00",
        "result_state": "reported_total_unverified",
        "instrument_title": "舒尔特方格",
        "reported_score": 21,
        "score_meaning": "source_reported_total_only",
    }


def assessment_record(ref="assessment_a_v1_abc", score=21):
    return {
        "assessment_ref": ref,
        "scale_type": 5,
        "scale_name": "GAD-7广泛性焦虑量表",
        "recorded_at": "2026-09-01T10:00:00+08:00",
        "result_state": "reported_total_unverified",
        "reported_score": score,
        "score_meaning": "source_reported_total_only",
    }


@pytest.mark.parametrize("endpoint", sorted(IRECOG_ENDPOINTS | ASSESSMENT_ENDPOINTS))
def test_new_endpoints_route_to_patient_base(endpoint):
    async def check():
        calls = []

        def respond(req):
            calls.append(req.url.path)
            return httpx.Response(200, json={})

        client = AIWebApiClient(
            AppTestSettings(
                dry_run=False,
                irecog_reports_enabled=True,
                assessment_reports_enabled=True,
            ),
            transport=httpx.MockTransport(respond),
        )
        try:
            await client.post(endpoint, {})
            assert calls == [f"/api/ai/patients/tools/{endpoint}"]
        finally:
            await client.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "endpoint,gate",
    [
        *[(e, "irecog_reports_enabled") for e in sorted(IRECOG_REPORT_ENDPOINTS)],
        *[(e, "assessment_reports_enabled") for e in sorted(ASSESSMENT_REPORT_ENDPOINTS)],
    ],
)
def test_report_endpoints_respect_settings_gates(endpoint, gate):
    async def check():
        client = AIWebApiClient(
            AppTestSettings(dry_run=False, **{gate: False}),
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})),
        )
        try:
            with pytest.raises(DomainError) as exc:
                await client.post(endpoint, {})
            assert exc.value.code == "capability_disabled"
        finally:
            await client.close()

    asyncio.run(check())


def test_agent_tool_specs_include_new_families():
    names = {tool.name for tool in build_tools()}
    assert IRECOG_ENDPOINTS <= names
    assert ASSESSMENT_ENDPOINTS <= names


def test_new_arg_schemas_reject_bad_selectors():
    with pytest.raises(ValidationError):
        IReCogSessionArgs(selector="session_ref")
    with pytest.raises(ValidationError):
        AssessmentSessionArgs(selector="assessment_ref")
    with pytest.raises(ValidationError):
        IReCogReportArgs(session_ref="")
    with pytest.raises(ValidationError):
        AssessmentReportArgs(assessment_ref="")
    assert AssessmentTrendArgs(scale_type=5).scale_type == 5
    with pytest.raises(ValidationError):
        AssessmentTrendArgs(scale_type=0)


def test_irecog_session_chain_and_facts():
    async def check():
        async with runtime(irecog_reports_enabled=True, assessment_reports_enabled=True) as (
            c,
            backend,
        ):
            backend.overrides["get_irecog_patient_history"] = envelope(
                "get_irecog_patient_history",
                "irecog-1.0.0",
                {
                    "sessions": [irecog_session()],
                    "page_number": 1,
                    "total_items": 1,
                    "has_next": False,
                },
            )
            backend.overrides["get_irecog_session_analysis"] = envelope(
                "get_irecog_session_analysis",
                "irecog-1.0.0",
                {"sessions": [irecog_session()], "comparability": "not_requested"},
            )
            run = await c.application.execute(request("解读最近IReCog测评", scope=SCOPE))
            names = [name for name, _ in backend.calls if "irecog" in name]
            assert names == ["get_irecog_patient_history", "get_irecog_session_analysis"]
            assert run.record.status == "succeeded"
            assert "舒尔特方格" in answers(run.record)
            assert "21" in answers(run.record)
            memory = await c.repository.conversation(SCOPE.thread_id("c1"))
            assert memory.current_record and memory.current_record.domain == "irecog"

    asyncio.run(check())


def test_irecog_report_passes_selector_and_ref():
    async def check():
        async with runtime(irecog_reports_enabled=True, assessment_reports_enabled=True) as (
            c,
            backend,
        ):
            backend.overrides["get_irecog_patient_history"] = envelope(
                "get_irecog_patient_history",
                "irecog-1.0.0",
                {
                    "sessions": [irecog_session()],
                    "page_number": 1,
                    "total_items": 1,
                    "has_next": False,
                },
            )
            backend.overrides["get_irecog_session_analysis"] = envelope(
                "get_irecog_session_analysis",
                "irecog-1.0.0",
                {"sessions": [irecog_session()], "comparability": "not_requested"},
            )
            backend.overrides["generate_irecog_single_session_report"] = artifact_envelope(
                "generate_irecog_single_session_report", "irecog-1.0.0"
            )
            run = await c.application.execute(request("解读最近IReCog测评并生成报告", scope=SCOPE))
            report = next(
                (name, body)
                for name, body in backend.calls
                if name == "generate_irecog_single_session_report"
            )
            assert report[1]["selector"] == "session_ref"
            assert report[1]["session_ref"] == "irecog_s_v1_abc"
            assert run.record.status == "succeeded"

    asyncio.run(check())


def test_assessment_session_and_report_snapshot_chain():
    async def check():
        async with runtime(irecog_reports_enabled=True, assessment_reports_enabled=True) as (
            c,
            backend,
        ):
            backend.overrides["get_assessment_patient_history"] = envelope(
                "get_assessment_patient_history",
                "assessment-1.0.0",
                {
                    "records": [assessment_record()],
                    "page_number": 1,
                    "has_next": False,
                    "source_snapshot": SNAPSHOT,
                },
            )
            backend.overrides["get_assessment_result_analysis"] = envelope(
                "get_assessment_result_analysis",
                "assessment-1.0.0",
                {
                    "records": [assessment_record()],
                    "comparability": "not_requested",
                    "source_snapshot": SNAPSHOT,
                },
            )
            backend.overrides["generate_assessment_single_report"] = artifact_envelope(
                "generate_assessment_single_report", "assessment-1.0.0"
            )
            run = await c.application.execute(request("解读最近量表测评并生成报告", scope=SCOPE))
            names = [name for name, _ in backend.calls if "assessment" in name]
            assert names == [
                "get_assessment_patient_history",
                "get_assessment_result_analysis",
                "generate_assessment_single_report",
            ]
            report = next(
                (name, body)
                for name, body in backend.calls
                if name == "generate_assessment_single_report"
            )
            assert report[1]["selector"] == "assessment_ref"
            assert report[1]["assessment_ref"] == "assessment_a_v1_abc"
            assert report[1]["expected_source_snapshot"] == SNAPSHOT
            assert run.record.status == "succeeded"
            assert "GAD-7" in answers(run.record)
            memory = await c.repository.conversation(SCOPE.thread_id("c1"))
            assert memory.current_record and memory.current_record.domain == "assessment"

    asyncio.run(check())


def test_assessment_trend_requires_two_to_twenty():
    async def check():
        async with runtime(irecog_reports_enabled=True, assessment_reports_enabled=True) as (
            c,
            backend,
        ):
            backend.overrides["get_assessment_longitudinal_analysis"] = envelope(
                "get_assessment_longitudinal_analysis",
                "assessment-1.0.0",
                {
                    "records": [assessment_record(f"assessment_a_v1_{i}", 20 - i) for i in range(4)],
                    "comparability": "blocked_unverified_scoring_rules",
                    "source_snapshot": SNAPSHOT,
                },
            )
            run = await c.application.execute(request("最近4次量表测评趋势", scope=SCOPE))
            names = [name for name, _ in backend.calls if "assessment" in name]
            assert names == ["get_assessment_longitudinal_analysis"]
            trend = next(
                (name, body)
                for name, body in backend.calls
                if name == "get_assessment_longitudinal_analysis"
            )
            assert trend[1]["selection_mode"] == "latest_count"
            assert trend[1]["report_count"] == 4
            assert run.record.status == "succeeded"

    asyncio.run(check())


def test_irecog_uses_project_patient_identity():
    async def check():
        async with runtime() as (c, backend):
            backend.overrides["get_irecog_patient_history"] = envelope(
                "get_irecog_patient_history",
                "irecog-1.0.0",
                {
                    "sessions": [irecog_session()],
                    "page_number": 1,
                    "total_items": 1,
                    "has_next": False,
                },
            )
            backend.overrides["get_irecog_session_analysis"] = envelope(
                "get_irecog_session_analysis",
                "irecog-1.0.0",
                {"sessions": [irecog_session()], "comparability": "not_requested"},
            )
            await c.application.execute(request("解读最近IReCog测评", scope=SCOPE))
            calls = [(name, body) for name, body in backend.calls if "irecog" in name]
            assert all(body["system_context"]["user"] == "3799" for _, body in calls)
            assert not any(name == "resolve_patient_identity" for name, _ in backend.calls)

    asyncio.run(check())
