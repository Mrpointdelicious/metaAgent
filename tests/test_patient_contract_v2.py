"""
创建日期：2026-10-29
文件功能：验证患者 v2 身份、范围隔离、机构事实和报告凭证边界。
"""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from langchain_core.messages import AIMessage

from meta_agent.agent.context import compact_result
from meta_agent.contracts import DomainError
from meta_agent.domains.facts import FactBuilder
from meta_agent.orchestration.identity import trusted_scope_from_inputs, validate_patient_envelope
from meta_agent.tools.ai_webapi import AIWebApiClient
from meta_agent.tools.demo import demo_response
from tests.helpers.identity import patient_inputs, patient_scope
from tests.helpers.runtime import request, runtime
from tests.helpers.settings import AppTestSettings
from tests.test_single_agent import activate, tool_call


@pytest.mark.parametrize(
    "key", ["patientId", "projectPatientId", "iretourPatientId", "patientPhone"]
)
def test_old_patient_identity_inputs_are_rejected(key):
    with pytest.raises(ValueError):
        trusted_scope_from_inputs({key: "7"}, "actor", "tenant")


def test_login_name_keeps_leading_zero_and_proof_rotation_preserves_scope():
    scope = trusted_scope_from_inputs(patient_inputs("000007", "login_name"), "actor", "tenant")
    assert scope.patient_identity.value == "000007"
    assert scope.identity_context_token not in repr(scope)
    renewed = replace(scope, identity_context_token="renewed-proof")
    assert renewed.scope_hash == scope.scope_hash
    assert (
        replace(scope, data_scope="current_tenant", current_business_id=1).scope_hash
        != scope.scope_hash
    )
    assert replace(scope, scope_fingerprint="new-binding").scope_hash != scope.scope_hash


def test_partial_binding_is_usable_and_changed_response_scope_is_rejected():
    scope = patient_scope("tenant", "actor", "7")
    body = demo_response("resolve_patient_identity", scope.patient_payload({}))
    body["status"] = "partial"
    body["query_scope"].update(coverage="partial", issues=["IDENTITY_LINK_AMBIGUOUS"])
    bound = scope.bind(body)
    assert bound.has_patient
    response = demo_response("get_patient_profile", bound.patient_payload({}))
    response["query_scope"]["scope_fingerprint"] = "other"
    with pytest.raises(DomainError) as error:
        validate_patient_envelope(bound, "get_patient_profile", response)
    assert error.value.code == "patient_scope_changed"


def test_fact_institution_is_from_its_record_and_tampering_is_rejected():
    async def check():
        async with runtime() as (container, _):
            scope = patient_scope("tenant", "actor", "7")
            body = demo_response("get_irego_session_analysis", scope.patient_payload({}))
            body["data"]["session"]["record_tenant"] = {
                "tenant_ref": "institution-a",
                "display_name": "A医院",
                "attribution_status": "verified",
            }
            evidence = await container.repository.save_evidence(
                scope.scope_hash, "get_irego_session_analysis", "request", body, "2.0.0"
            )
            builder = FactBuilder(evidence)
            builder.metrics(body["data"]["report_blocks"])
            metric = next(
                view for view in builder.views if view.fact.semantic_key == "walking_speed"
            )
            assert metric.fact.record_tenant["display_name"] == "A医院"
            assert metric.fact.scope_fingerprint == scope.scope_fingerprint
            assert await container.repository.verify_fact(scope.scope_hash, metric)
            forged = metric.model_copy(deep=True)
            forged.fact.record_tenant["display_name"] = "B卫生中心"
            assert not await container.repository.verify_fact(scope.scope_hash, forged)

    asyncio.run(check())


def test_download_grants_do_not_enter_model_tool_messages():
    body = {
        "data": {
            "image_url": "https://example.test/r?access_grant=secret",
            "display_markdown": "![report](https://example.test/r?access_grant=secret)",
            "source_snapshot": "snapshot",
        },
        "system_context": {"identity_context_token": "proof"},
    }
    compact = json.dumps(compact_result(body, 1000))
    assert "secret" not in compact and "proof" not in compact and "system_context" not in compact
    assert "snapshot" in compact


@pytest.mark.parametrize("domain", ["irego", "iretour", "irecog", "assessment"])
def test_each_report_binds_the_fresh_analysis_snapshot(domain):
    async def check():
        async with runtime(
            iretour_reports_enabled=True,
            irecog_reports_enabled=True,
            assessment_reports_enabled=True,
        ) as (container, backend):
            scope = patient_scope("tenant", "actor", "7")
            assessment = domain == "assessment"
            key = "assessment_ref" if assessment else "session_ref"
            reference = f"{domain}_a_v1_example" if assessment else f"{domain}_s_v2_example"
            analysis = (
                "get_assessment_result_analysis" if assessment else f"get_{domain}_session_analysis"
            )
            report = (
                "generate_assessment_single_report"
                if assessment
                else f"generate_{domain}_single_session_report"
            )
            row = {key: reference}
            data = {"source_snapshot": "fresh-snapshot"}
            data.update(
                {"records": [row]}
                if assessment
                else {"sessions": [row]}
                if domain == "irecog"
                else {"session": row}
            )
            backend.overrides[analysis] = {
                "tool_name": analysis,
                "contract_version": "2.0.0",
                "request_id": "r",
                "status": "success",
                "meta": {},
                "data": data,
            }
            backend.overrides[report] = {
                "tool_name": report,
                "contract_version": "2.0.0",
                "request_id": "r",
                "status": "available",
                "meta": {},
                "data": {
                    "artifact_ref": "report",
                    "image_url": "https://images.example.test/r?access_grant=fresh",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                },
            }
            activate(container, [tool_call(report, {key: reference}), AIMessage("报告已生成。")])
            run = await container.application.execute(request("生成报告", scope=scope))
            calls = [(name, args) for name, args in backend.calls if name in {analysis, report}]
            assert [name for name, _ in calls] == [analysis, report]
            assert calls[-1][1]["expected_source_snapshot"] == "fresh-snapshot"
            assert any(event.type == "artifact_ready" for event in run.record.events)
            memory = await container.repository.conversation(scope.thread_id("c1"))
            assert "access_grant=fresh" not in json.dumps(memory.agent_messages)

    asyncio.run(check())


def test_http_errors_preserve_business_reason_and_bound_the_response():
    async def check():
        client = AIWebApiClient(
            AppTestSettings(dry_run=False),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(403, json={"reason_codes": ["PATIENT_IDENTITY_MISMATCH"]})
            ),
        )
        try:
            with pytest.raises(DomainError) as error:
                await client.post("resolve_patient_identity", {})
            assert error.value.code == "PATIENT_IDENTITY_MISMATCH"
            assert not error.value.retryable
        finally:
            await client.close()

    asyncio.run(check())
