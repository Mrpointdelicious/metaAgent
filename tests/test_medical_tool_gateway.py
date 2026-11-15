"""
创建日期：2026-11-15
文件功能：验证工具分层后的参数/封套透传、原有认证边界与提供方隔离。
"""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from meta_agent.medical_tools.adapters.platform import doctors, scene
from meta_agent.medical_tools.contracts import CONTRACT_VERSION, ToolContext
from meta_agent.medical_tools.errors import ToolError
from meta_agent.medical_tools.gateway import MedicalToolGateway
from meta_agent.orchestration.identity import TrustedScope
from meta_agent.tools.ai_webapi import AIWebApiClient
from meta_agent.tools.demo import demo_response
from tests.helpers.identity import patient_scope
from tests.helpers.settings import AppTestSettings


@pytest.mark.parametrize("name", ["navigate_scene", "search_doctors"])
def test_platform_contract_preserves_original_arguments_and_envelope(name, monkeypatch):
    """统一层不得补位置、改字段或把旧封套重组为另一种输出。"""
    body = (
        {"status": 200, "data": {"spaceId": "space", "version": 7, "commands": []}}
        if name == "navigate_scene"
        else doctors.build_envelope(["张医生", "李医生"], ["1", "2"], ["139", "138"])
    )
    body["extension"] = {"keep": [1, 2]}
    arguments = {"spaceId": "space"}
    if name == "navigate_scene":
        arguments.update(target="去四楼", expectedSceneVersion=7)
    seen = []

    async def resolve(client, settings, payload):
        seen.append(payload)
        return body

    module, resolver = (
        (scene, "resolve_local_scene")
        if name == "navigate_scene"
        else (doctors, "resolve_local_doctors")
    )
    monkeypatch.setattr(module, resolver, resolve)

    async def check():
        gateway = MedicalToolGateway(AppTestSettings(dry_run=False))
        try:
            result = await gateway.execute(
                name,
                arguments,
                ToolContext(TrustedScope("tenant", "user", position="三楼", space_id="space")),
            )
            assert result.tool_name == name
            assert result.contract_version == CONTRACT_VERSION
            assert result.domain_payload() is body
            assert seen == [arguments]
        finally:
            await gateway.close()

    asyncio.run(check())


def test_patient_identity_and_report_metadata_pass_through_existing_v2_contract():
    async def check():
        calls, responses = [], []

        def handler(request):
            payload = json.loads(request.content)
            calls.append((request, payload))
            name = request.url.path.rsplit("/", 1)[-1]
            body = demo_response(name, payload)
            body["meta"]["watermark"] = "original-watermark"
            body["extension"] = {"unchanged": True}
            responses.append(body)
            return httpx.Response(200, json=body)

        settings = AppTestSettings(
            dry_run=False,
            ai_webapi_bearer_token="",
            ai_webapi_base_url="https://webapi.test/api/ai/patients/tools",
        )
        gateway = MedicalToolGateway(settings, transport=httpx.MockTransport(handler))
        try:
            scope = replace(
                patient_scope("tenant", "user"), patient_ref=None, scope_fingerprint=None
            )
            scope = await gateway.resolve_patient_identity(scope)
            result = await gateway.execute(
                "generate_irego_single_session_report",
                {"session_ref": "session-1", "expected_source_snapshot": "original-snapshot"},
                ToolContext(scope, request_id="request-1", conversation_id="conversation-1"),
            )
            assert result.domain_payload() == responses[-1]
            assert result.domain_payload()["contract_version"] == "2.0.0"
            assert calls[1][1]["system_context"] == {
                "user": "user",
                "conversation_id": "conversation-1",
                "patient_identity": scope.patient_identity.model_dump(),
                "current_business_id": None,
                "identity_context_token": "synthetic-proof-only",
            }
            assert calls[1][1]["expected_source_snapshot"] == "original-snapshot"
            assert calls[1][1]["data_scope"] == scope.data_scope
            assert all("Authorization" not in request.headers for request, _ in calls)
        finally:
            await gateway.close()

    asyncio.run(check())


def test_hospital_directory_and_query_share_gateway_keep_get_post_and_existing_header():
    async def check():
        calls = []
        entries = [{"hospital_id": 1, "hospital_name": "合成医院"}]

        def handler(request):
            calls.append(request)
            if request.method == "GET":
                return httpx.Response(200, json=entries)
            return httpx.Response(200, json=demo_response("query_hospital_operations", {}))

        gateway = MedicalToolGateway(
            AppTestSettings(
                dry_run=False,
                ai_webapi_hospital_bearer_token="existing-hospital-token",
                ai_webapi_hospital_base_url="https://webapi.test/api/ai/hospital-operations/tools",
            ),
            transport=httpx.MockTransport(handler),
        )
        try:
            directory = await gateway.execute("query_hospital_directory", {})
            assert directory.domain_payload() == {"directory": entries}
            arguments = {"hospital_id": 1, "scope": "institution"}
            await gateway.execute("query_hospital_operations", arguments)
            assert [(r.method, r.url.path) for r in calls] == [
                ("GET", "/api/ai/hospital-operations/directory"),
                ("POST", "/api/ai/hospital-operations/tools/query_hospital_operations"),
            ]
            assert json.loads(calls[1].content) == arguments
            assert all(
                r.headers["Authorization"] == "Bearer existing-hospital-token" for r in calls
            )
        finally:
            await gateway.close()

    asyncio.run(check())


@pytest.mark.parametrize("code", [401, 403])
@pytest.mark.parametrize("http_status", [False, True])
def test_doctor_auth_errors_keep_original_mapping_without_extra_calls(code, http_status):
    async def check():
        doctors.reset_token_cache()
        calls = []

        def handler(request):
            calls.append(request)
            if request.url.path == "/login":
                return httpx.Response(200, json={"token": "original-token"})
            return httpx.Response(code if http_status else 200, json={"code": code})

        gateway = MedicalToolGateway(
            AppTestSettings(
                dry_run=False,
                doctor_backend_login_url="https://platform.test/login",
                doctor_backend_query_url="https://platform.test/doctors",
                doctor_login_phone="synthetic-account",
                doctor_login_password="synthetic-password",
            ),
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(ToolError) as caught:
                await gateway.execute("search_doctors", {"spaceId": "space"})
            assert caught.value.code == (
                f"http_{code}" if http_status else "doctor_upstream_unauthorized"
            )
            assert caught.value.outcome == "unavailable"
            assert caught.value.message == "在线医生服务暂时不可用。"
            assert [r.url.path for r in calls] == ["/login", "/doctors"]
            assert calls[1].headers["Authorization"] == "Bearer original-token"
        finally:
            await gateway.close()
            doctors.reset_token_cache()

    asyncio.run(check())


def test_webapi_transport_no_longer_routes_platform_tools():
    async def check():
        calls = []
        body = demo_response("navigate_scene", {"target": "去四楼", "spaceId": "space"})

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=body)

        client = AIWebApiClient(
            AppTestSettings(
                dry_run=False,
                scene_local_enabled=True,
                ai_webapi_rehab_base_url="https://webapi.test/api/ai/rehab/tools",
            ),
            transport=httpx.MockTransport(handler),
        )
        try:
            assert await client.post("navigate_scene", {"target": "去四楼"}) == body
            assert [r.url.path for r in calls] == ["/api/ai/rehab/tools/navigate_scene"]
        finally:
            await client.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "name,code",
    [
        ("unregistered_tool", "unknown_tool"),
        ("get_multisource_patient_context", "capability_disabled"),
        ("generate_iretour_single_session_report", "capability_disabled"),
    ],
)
def test_registry_rejects_unknown_or_disabled_tools_before_transport(name, code):
    async def check():
        def handler(request):
            pytest.fail("disabled tools must not call an upstream")

        gateway = MedicalToolGateway(
            AppTestSettings(dry_run=False), transport=httpx.MockTransport(handler)
        )
        try:
            with pytest.raises(ToolError) as caught:
                await gateway.execute(name, {})
            assert caught.value.code == code
        finally:
            await gateway.close()

    asyncio.run(check())
