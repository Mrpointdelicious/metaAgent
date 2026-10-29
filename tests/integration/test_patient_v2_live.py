"""
创建日期：2026-10-29
文件功能：按显式本地配置联调真实 AI_WebApi v2、报告及 MetaAgent HTTP 入口。
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import uvicorn
from langchain_core.messages import AIMessage

from meta_agent.app import create_app
from meta_agent.contracts import DomainError
from meta_agent.orchestration.identity import (
    PatientIdentity,
    TrustedScope,
    validate_patient_envelope,
)
from meta_agent.tools.ai_webapi import AIWebApiClient
from tests.helpers.settings import AppTestSettings
from tests.test_single_agent import ScriptedModel, tool_call

CONFIG = os.environ.get("META_AGENT_LIVE_PATIENT_V2_CONFIG")
pytestmark = pytest.mark.skipif(
    not CONFIG, reason="explicit local v2 integration configuration required"
)


@pytest.fixture
def live_config():
    return json.loads(Path(CONFIG).read_text(encoding="utf-8"))


def settings(config, **changes):
    return AppTestSettings(
        dry_run=False,
        ai_webapi_base_url=config["base_url"],
        ai_webapi_bearer_token=config["bearer_token"],
        persistence_backend="memory",
        iretour_reports_enabled=True,
        irecog_reports_enabled=True,
        assessment_reports_enabled=True,
        **changes,
    )


def signed_scope(config, *, data_scope="all_tenants", patient=None, seconds=600):
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    patient = patient or config["patient_id"]
    current = config["current_business_id"]
    claims = {
        "purpose": "patient_rehab_v2",
        "subject": "v2-integration-patient",
        "anchor_user_id": int(patient),
        "actor_role": "patient",
        "current_business_id": current,
        "allow_all_tenants": True,
        "phone_binding_hash": None,
        "issued_at": int(time.time()),
        "expires_at": int(time.time()) + seconds,
    }
    message = "patient_identity_v2." + encode(json.dumps(claims, separators=(",", ":")).encode())
    proof = (
        message
        + "."
        + encode(
            hmac.new(
                config["identity_signing_key"].encode(), message.encode(), hashlib.sha256
            ).digest()
        )
    )
    return TrustedScope(
        "integration",
        claims["subject"],
        PatientIdentity(type="project_user_id", value=str(patient)),
        current_business_id=current,
        data_scope=data_scope,
        identity_context_token=proof,
    )


async def read(client, scope, endpoint, arguments=None):
    body = await client.post(endpoint, scope.patient_payload(arguments or {}, "integration"))
    validate_patient_envelope(scope, endpoint, body)
    return body


@pytest.mark.parametrize(
    "endpoint",
    [
        "get_patient_profile",
        "get_patient_consultation",
        "get_patient_rehab",
        "get_irego_patient_history",
        "get_iretour_patient_context",
        "get_irecog_patient_context",
        "get_assessment_patient_context",
    ],
)
def test_real_patient_families_use_the_same_contract_and_scope(live_config, endpoint):
    async def check():
        client = AIWebApiClient(settings(live_config))
        try:
            scope = await client.resolve_patient_identity(signed_scope(live_config))
            body = await read(client, scope, endpoint)
            assert body["contract_version"] == "2.0.0"
            assert "identity_context_token" not in json.dumps(body)
        finally:
            await client.close()

    asyncio.run(check())


def test_all_tenants_and_current_tenant_counts_and_attribution(live_config):
    async def check():
        client = AIWebApiClient(settings(live_config))
        try:
            counts = []
            for selection in ["all_tenants", "current_tenant"]:
                scope = await client.resolve_patient_identity(
                    signed_scope(live_config, data_scope=selection)
                )
                body = await read(client, scope, "get_patient_rehab", {"page_size": 20})
                data = body["data"]
                counts.append(data["plan_count"])
                plans = data["plans"]
                assert plans and all(plan.get("record_tenant") for plan in plans)
                assert body.get("tenant_segments")
                if selection == "current_tenant":
                    current = body["query_scope"]["current_tenant_ref"]
                    assert all(p["record_tenant"]["tenant_ref"] == current for p in plans)
                    assert all(
                        p["record_tenant"]["attribution_status"] == "verified" for p in plans
                    )
                else:
                    assert len({p["record_tenant"]["tenant_ref"] for p in plans}) > 1
            assert counts[0] > counts[1] > 0
            if "expected_plan_counts" in live_config:
                assert counts == live_config["expected_plan_counts"]
        finally:
            await client.close()

    asyncio.run(check())


def test_real_report_snapshot_download_and_scope_enforcement(live_config):
    async def check():
        client = AIWebApiClient(settings(live_config))
        try:
            scope = await client.resolve_patient_identity(signed_scope(live_config))
            analysis = await read(
                client,
                scope,
                "get_irego_session_analysis",
                {"detail_level": "standard", "quality_detail": "summary"},
            )
            data = analysis["data"]
            assert data and data["source_snapshot"] and data["session"]["record_tenant"]
            reference = data["session"]["session_ref"]
            report = await read(
                client,
                scope,
                "generate_irego_single_session_report",
                {"session_ref": reference, "expected_source_snapshot": data["source_snapshot"]},
            )
            assert report["status"] in {"available", "success", "partial"}, report.get(
                "reason_codes"
            )
            assert report["data"]["tenant_segments"]
            url = report["data"]["image_url"]
            assert "access_grant=" in url
            assert await client.artifact_available(url)
            response = await client.client.get(url)
            assert response.content.startswith(b"\x89PNG")
            evidence = Path(CONFIG).parent / "irego-v2-report.png"
            evidence.write_bytes(response.content)
            assert (await client.client.get(url.split("?", 1)[0])).status_code == 401
            changed = await read(
                client,
                scope,
                "generate_irego_single_session_report",
                {"session_ref": reference, "expected_source_snapshot": "stale"},
            )
            assert (
                changed["status"] == "unavailable"
                and "IREGO_SOURCE_CHANGED" in changed["reason_codes"]
            )
            tenant = await client.resolve_patient_identity(
                signed_scope(live_config, data_scope="current_tenant")
            )
            with pytest.raises(DomainError) as denied:
                await read(
                    client,
                    tenant,
                    "get_irego_session_analysis",
                    {"selector": "session_ref", "session_ref": reference},
                )
            assert not denied.value.retryable
        finally:
            await client.close()

    asyncio.run(check())


def test_expired_and_mismatched_identity_proofs_are_rejected(live_config):
    async def check():
        client = AIWebApiClient(settings(live_config))
        try:
            expired = signed_scope(live_config, seconds=-1)
            with pytest.raises(DomainError) as error:
                await client.resolve_patient_identity(expired)
            assert error.value.code == "PATIENT_IDENTITY_PROOF_INVALID"
            wrong = replace(
                signed_scope(live_config),
                patient_identity=PatientIdentity(
                    type="project_user_id", value=live_config["other_patient_id"]
                ),
            )
            with pytest.raises(DomainError) as error:
                await client.resolve_patient_identity(wrong)
            assert error.value.code == "PATIENT_IDENTITY_MISMATCH"
        finally:
            await client.close()

    asyncio.run(check())


def test_real_metaagent_http_calls_real_webapi_without_exposing_credentials(
    live_config, monkeypatch
):
    # Only the language model is scripted: identity, tool HTTP, scopes and report storage are real.
    model = ScriptedModel(
        messages=iter(
            [tool_call("get_patient_rehab"), AIMessage("已读取康复记录，记录保留所属机构。")]
        )
    )
    monkeypatch.setattr("meta_agent.infrastructure.container.create_planner_model", lambda _: model)
    config = settings(
        live_config, orchestration_mode="agent", service_bearer_token="integration-only"
    )
    app = create_app(config)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        scope = signed_scope(live_config)
        inputs = {
            "patient_identity": scope.patient_identity.model_dump(),
            "identity_context_token": scope.identity_context_token,
            "current_business_id": scope.current_business_id,
            "data_scope": scope.data_scope,
        }
        with httpx.Client(
            base_url=f"http://127.0.0.1:{sock.getsockname()[1]}",
            timeout=60,
            headers={"Authorization": "Bearer integration-only"},
        ) as http:
            request = {
                "user": scope.end_user_id,
                "inputs": inputs,
                "request_id": "real-v2-http",
                "query": "查询我的全部机构康复记录",
                "conversation_id": "v2-http",
                "response_mode": "blocking",
            }
            response = http.post("/v1/chat", json=request)
            assert response.status_code == 200
            body = response.json()
            assert body["status"] in {"succeeded", "partial"}
            assert body["metrics"]["tool_calls"] == 2
            assert scope.identity_context_token not in response.text
            status = http.post(
                f"/v1/runs/{body['run_id']}/status",
                json={"user": scope.end_user_id, "inputs": inputs},
            )
            assert status.status_code == 200
            changed = http.post(
                f"/v1/runs/{body['run_id']}/status",
                json={
                    "user": scope.end_user_id,
                    "inputs": {**inputs, "data_scope": "current_tenant"},
                },
            )
            assert changed.status_code == 404
            assert (
                http.post("/v1/chat", json={**request, "inputs": {"patientId": "7"}}).status_code
                == 422
            )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    assert not thread.is_alive()
