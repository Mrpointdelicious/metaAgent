"""
创建日期：2026-11-23
文件功能：验证四种角色在模型、执行端、会话及访问业务资源时的隔离。
"""

import asyncio
import json
import time
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from meta_agent.agent.tools import available_tools, build_tools, invoke_tool
from meta_agent.api.security import ClientIdentity
from meta_agent.app import create_app
from meta_agent.application.service import ApplicationRequest
from meta_agent.config import ClientTokenConfig
from meta_agent.contracts import DomainError, TaskSpec
from meta_agent.domains.hospital import render_hospital_markdown
from meta_agent.execution.dispatcher import DomainDispatcher
from meta_agent.flows.consultation_room.subtasks import route
from meta_agent.medical_tools.contracts import ToolContext
from meta_agent.medical_tools.gateway import MedicalToolGateway
from meta_agent.orchestration.identity import TrustedScope
from meta_agent.orchestration.resources import verify_resource
from meta_agent.roles import ROLE_AGENTS
from tests.helpers.identity import (
    clinical_flow_scope,
    clinical_scope,
    operator_scope,
    patient_scope,
)
from tests.helpers.integration import context
from tests.helpers.runtime import runtime
from tests.helpers.settings import AppTestSettings
from tests.test_consultation_sessions import activate, category


@pytest.mark.parametrize(
    ("role", "agent", "prompt", "permitted", "forbidden"),
    [
        ("patient", "patient_rehab", "康复科老医生", "search_doctors", "get_patient_profile"),
        (
            "clinician",
            "clinician_workspace",
            "医生的问诊助手",
            "open_chat_history",
            "search_doctors",
        ),
        ("student", "student_tutor", "学生的康复学习助手", None, "navigate_scene"),
        ("operator", "hospital_ops", "机构分析助手", "query_hospital_operations", "navigate_scene"),
    ],
)
def test_each_role_runs_its_own_profile(role, agent, prompt, permitted, forbidden):
    async def check():
        async with runtime() as (container, backend):
            model = activate(container, ["本轮回答"])
            scope = (
                operator_scope() if role == "operator" else TrustedScope("t", "actor", role=role)
            )
            run = await container.application.execute(
                ApplicationRequest(
                    "介绍一下你能做什么",
                    scope,
                    "",
                    "role-1",
                    raw_inputs={"pos": "问诊房间" if role in {"operator", "student"} else "大厅"},
                )
            )
            assert run.record.status == "succeeded"
            assert run.record.metrics["agent_id"] == agent
            assert run.record.metrics["profile_version"] == ROLE_AGENTS.for_role(role).version
            assert run.record.metrics["branch"] == "default"
            assert prompt in model.seen[0]
            if role != "patient":
                assert "康复科老医生" not in model.seen[0]
            tools = {tool.name for tool in available_tools(run.context, build_tools())}
            assert forbidden not in tools and (not permitted or permitted in tools)
            assert not backend.calls and not run.record.action_delivery

    asyncio.run(check())


@pytest.mark.parametrize(
    ("role", "operation"),
    [
        ("student", "query_hospital_directory"),
        ("clinician", "get_patient_profile"),
        ("operator", "search_doctors"),
        ("student", "navigate_scene"),
    ],
)
def test_gateway_denies_out_of_profile_before_transport(role, operation):
    async def check():
        async with runtime() as (container, backend):
            backend.directory = AsyncMock()
            gateway = MedicalToolGateway.wrap(backend, container.settings)
            scope = operator_scope() if role == "operator" else TrustedScope("t", "user", role=role)
            with pytest.raises(DomainError):
                await gateway.execute(operation, {}, ToolContext(scope))
            with pytest.raises(DomainError, match="可信运行上下文"):
                await gateway.execute(operation, {})
            assert not backend.calls
            backend.directory.assert_not_awaited()

    asyncio.run(check())


def test_legacy_dispatch_and_clinical_handlers_check_without_model_tools():
    async def check():
        async with runtime() as (container, backend):
            backend.directory = AsyncMock()
            student = context(container, scope=TrustedScope("t", "user", role="student"))
            task = TaskSpec(
                task_id="metrics",
                goal_ids=["goal"],
                capability="hospital.query",
                idempotency_key="metrics",
            )
            with pytest.raises(DomainError, match="不允许"):
                await DomainDispatcher().execute(task, {"hospital_id": 1}, student)
            participant = clinical_scope(
                scope=patient_scope("tenant", "actor"), capabilities={"clinical.invite"}
            )
            for scope in (
                participant,
                clinical_scope(capabilities={"clinical.invite"}),
                TrustedScope("tenant", "doctor", role="clinician"),
            ):
                ctx = context(container, scope=replace(scope, agent_kind="consultation_room"))
                with pytest.raises(DomainError, match="不允许"):
                    await route(ctx, "医嘱填写", "", container.application)
                assert not ctx.record.events and not ctx.record.action_delivery
            backend.directory.assert_not_awaited()
            assert not backend.calls

    asyncio.run(check())


def test_operator_limits_both_queries_and_returned_data():
    async def check():
        async with runtime() as (container, backend):
            scope = operator_scope(sections=("rooms",))
            ctx = context(container, scope=scope)
            backend.directory_entries.append({"hospital_id": 2, "hospital_name": "其他机构"})
            directory = await invoke_tool(ctx, "query_hospital_directory", {})
            assert [row["hospital_id"] for row in directory["data"]["directory"]] == [1]
            directory_none = await invoke_tool(ctx, "query_hospital_directory", {"name": None})
            assert [row["hospital_id"] for row in directory_none["data"]["directory"]] == [1]
            for args in (
                {"hospital_id": 2, "section": "rooms"},
                {"scope": "platform"},
                {"scope": "comparison", "hospital_ids": [1, 2]},
                {"section": "records"},
                {"section": "rooms", "output_mode": "report"},
            ):
                await invoke_tool(ctx, "query_hospital_operations", args)
                assert list(ctx.record.results.values())[-1].code == "resource_denied"
            assert not backend.calls
            gateway = MedicalToolGateway.wrap(backend, container.settings)
            with pytest.raises(DomainError, match="授权目录"):
                await gateway.execute(
                    "query_hospital_operations",
                    {"section": "rooms", "hospital_name": "其他机构"},
                    ToolContext(scope),
                )
            with pytest.raises(DomainError, match="参数无效"):
                await gateway.execute(
                    "query_hospital_operations",
                    {"section": "rooms", "other_target": 2},
                    ToolContext(scope),
                )
            assert not backend.calls
            body = {
                "tool_name": "query_hospital_operations",
                "status": "success",
                "data": {
                    "scope": "institution",
                    "section": "rooms",
                    "period": {"patient_names": ["restricted"]},
                    "hospitals": [
                        {
                            "hospital": {"hospital_id": 1, "hospital_name": "合成医院"},
                            "rooms": {"total_rooms": 2, "patient_names": ["restricted"]},
                            "records": {"text": "restricted"},
                        }
                    ],
                    "analysis": [{"text": "restricted"}],
                    "report": {"image_url": "restricted"},
                },
            }
            backend.overrides["query_hospital_operations"] = body
            result = await gateway.execute(
                "query_hospital_operations", {"section": "rooms"}, ToolContext(scope)
            )
            assert "restricted" not in str(result.payload) and "total_rooms" in str(result.payload)
            assert backend.calls[-1][1]["hospital_id"] == 1
            assert "| 诊室合计 | 2 间 |" in render_hospital_markdown(result.payload)
            body["data"]["hospitals"][0]["rooms"] = {"total_rooms": None, "training_rooms": 0}
            result = await gateway.execute(
                "query_hospital_operations", {"section": "rooms"}, ToolContext(operator_scope())
            )
            rendered = render_hospital_markdown(result.payload)
            assert "| 训练室 | 0 间 |" in rendered and "诊室合计" not in rendered
            assert result.payload["data"]["report"] is None
            body["data"]["hospitals"][0]["hospital"]["hospital_id"] = 2
            with pytest.raises(DomainError, match="上游未确认"):
                await gateway.execute(
                    "query_hospital_operations", {"section": "rooms"}, ToolContext(scope)
                )

    asyncio.run(check())


def test_operator_gateway_and_role_specific_disable(settings):
    settings = settings.model_copy(
        update={
            "disabled_agent_roles": frozenset({"student"}),
        }
    )
    settings = AppTestSettings(
        **{
            **settings.model_dump(),
            "service_bearer_token_operator": "backoffice-test",
        }
    )
    with TestClient(create_app(settings)) as client:

        def chat(role, token):
            return client.post(
                "/v1/chat-messages",
                json={
                    "query": "你好",
                    "user": "actor",
                    "response_mode": "blocking",
                    "inputs": {"role": role} if role else {},
                },
                headers={"Authorization": "Bearer " + token},
            )

        assert chat(None, "test-service-token").status_code == 422
        assert chat("operator", "test-service-token").status_code == 403
        assert chat("patient", "backoffice-test").status_code == 403
        assert chat("operator", "backoffice-test").status_code == 200
        assert chat("student", "test-service-token").status_code == 503
        assert chat("patient", "test-service-token").status_code == 200


def test_verified_visit_scopes_history_status_cancel_and_replay(monkeypatch):
    settings = AppTestSettings(
        service_bearer_token="front-test",
        resource_authorization_token="auth-test",
        resource_authorization_url="https://authority.test/check",
    )
    granted = {"proof-a": clinical_scope(), "proof-b": clinical_scope("visit-b", "target-b")}
    checked = []

    def verify(req):
        body = json.loads(req.content)
        assert req.headers["Authorization"] == "Bearer auth-test"
        checked.append(body)
        scope = granted.get(body["resource_context_token"])
        if not scope:
            return httpx.Response(403)
        grant = scope.resource_grant
        return httpx.Response(
            200,
            json={
                **grant.model_dump(mode="json"),
                "patient_phone": grant.patient_phone.get_secret_value(),
                "requester_phone": grant.requester_phone.get_secret_value(),
                "encounter_summary": grant.encounter_summary.get_secret_value(),
            },
        )

    monkeypatch.setattr(
        "meta_agent.api.routes.verify_resource",
        partial(verify_resource, transport=httpx.MockTransport(verify)),
    )
    with TestClient(create_app(settings), headers={"Authorization": "Bearer front-test"}) as client:
        container = client.app.state.container
        model = activate(container, [category("结束问诊"), category(), "另一就诊的回答"])

        def access(visit, credential):
            return {
                "user": "doctor",
                "inputs": {
                    "tenant_id": "tenant",
                    "pos": "问诊房间",
                    "role": "clinician",
                    "consultation_id": visit,
                    "resource_context_token": credential,
                },
            }

        first = access("visit-a", "proof-a")
        body = {
            **first,
            "query": "结束当前问诊",
            "request_id": "visited",
            "response_mode": "blocking",
        }
        result = client.post("/v1/chat", json=body).json()
        assert result["status"] == "succeeded"
        cid, run = result["conversation_id"], result["run_id"]
        assert result["metrics"]["agent_id"] == "clinician_workspace"
        messages = "/v1/messages/query?conversation_id=" + cid
        assert client.post(messages, json=first).json()["data"]
        other = access("visit-b", "proof-b")
        for url in (messages, f"/v1/runs/{run}/status", f"/v1/runs/{run}/cancel"):
            assert client.post(url, json=other).status_code == 404
            assert client.post(url, json=access("visit-b", "proof-a")).status_code == 403
        assert (
            client.get(
                "/v1/messages",
                params={
                    "user": "doctor",
                    "role": "clinician",
                    "tenant_id": "tenant",
                    "conversation_id": cid,
                },
            ).status_code
            == 404
        )
        mismatch = {**body, "inputs": {**first["inputs"], "patientphone": "different"}}
        assert (
            client.post("/v1/chat", json={**mismatch, "request_id": "mismatch"}).status_code == 403
        )
        renewed = client.post("/v1/chat", json=body).json()
        assert renewed["reused"] and renewed["run_id"] == run
        assert "end_consultation" in json.dumps(result["events"])
        assert "end_consultation" not in json.dumps(renewed["events"])
        second = client.post(
            "/v1/chat", json={**body, **other, "query": "新的就诊", "conversation_id": cid}
        ).json()
        assert second["conversation_id"] != cid and second["run_id"] != run
        assert "结束当前问诊" not in model.seen[-1]
        assert "proof-a" not in json.dumps(result) and "13000000002" not in json.dumps(result)
        granted.pop("proof-a")
        assert client.post("/v1/chat", json=body).status_code == 403
        assert client.post(messages, json=first).status_code == 403
        assert checked and all("patientphone" not in row for row in checked)


def test_revoked_grant_cannot_be_used_in_an_existing_run():
    async def check():
        async with runtime() as (container, backend):
            grant = clinical_scope().resource_grant.model_copy(
                update={"expires_at": int(time.time()) - 1}
            )
            ctx = context(
                container,
                scope=replace(
                    clinical_scope(), agent_kind="consultation_room", resource_grant=grant
                ),
            )
            with pytest.raises(DomainError, match="不允许"):
                await route(ctx, "病历填写", "", SimpleNamespace())
            assert not ctx.business_inputs and not backend.calls and not ctx.record.events

    asyncio.run(check())


def test_switching_organization_separates_ids_history_and_cached_results():
    async def check():
        async with runtime() as (container, backend):
            app = container.application
            first = await app.execute(
                ApplicationRequest("汇总上月本机构门诊量", operator_scope(), "", "report")
            )
            assert first.record.status == "succeeded"
            assert backend.calls[-1][1]["date_mode"] == "last_month"
            other = await app.execute(
                ApplicationRequest(
                    "汇总上月本机构门诊量",
                    operator_scope(2),
                    first.record.conversation_id,
                    "report",
                )
            )
            assert other.record.status == "succeeded" and other.record.run_id != first.record.run_id
            assert other.record.conversation_id != first.record.conversation_id
            assert other.record.scope_key != first.record.scope_key
            assert backend.calls[-1][1]["hospital_id"] == 2
            assert len(other.context.memory.turns) == 1
            assert await app.accessible_run(other.record.scope_key, first.record.run_id) is None

    asyncio.run(check())


def test_no_unsafe_scene_fallback_in_a_consultation():
    from tests.helpers.runtime import scene_response
    from tests.test_single_agent import tool_call

    async def check():
        async with runtime(scene_actions_enabled=True) as (container, backend):
            activate(
                container,
                [
                    category("指令功能"),
                    tool_call("navigate_scene", {"target": "打开个人中心"}),
                    "无法执行该点位。",
                ],
            )
            backend.overrides["navigate_scene"] = scene_response
            run = await container.application.execute(
                ApplicationRequest(
                    "打开个人中心",
                    TrustedScope("tenant", "actor", space_id="space", scene_version=7),
                    "",
                    "unpublished",
                    raw_inputs={"pos": "问诊房间"},
                )
            )
            assert any(
                dispatch["code"] == "scene_control_not_public"
                for row in run.record.results.values()
                for dispatch in row.outputs.get("dispatches", [])
            )
            assert not run.record.action_delivery
            assert all(event.type != "action_ready" for event in run.record.events)

    asyncio.run(check())


def test_resource_verifier_is_optional_for_chat_but_required_for_a_presented_token(
    client, auth_headers
):
    result = client.post(
        "/v1/chat-messages",
        headers=auth_headers,
        json={
            "query": "填写病历",
            "user": "actor",
            "response_mode": "blocking",
            "inputs": {
                "role": "clinician",
                "consultation_id": "visit",
                "resource_context_token": "opaque",
            },
        },
    )
    assert (
        result.status_code == 503
        and result.json()["detail"]["code"] == "resource_verifier_unavailable"
    )


def test_fixed_summary_token_checks_role_visit_and_location_before_model(settings, monkeypatch):
    settings.service_clients = [
        ClientTokenConfig(name="visit-summary", token="test-summary-only", flow="ai_summary")
    ]
    scope = clinical_flow_scope("ai_summary", client_id="visit-summary")

    async def authorize(request_scope, token, settings):
        assert token == "proof-a" and scope.resource_grant.valid_for(request_scope)
        return replace(request_scope, resource_grant=scope.resource_grant)

    monkeypatch.setattr("meta_agent.api.routes.verify_resource", authorize)
    headers = {"Authorization": "Bearer test-summary-only"}
    with TestClient(create_app(settings), headers=headers) as client:
        model = activate(
            client.app.state.container, ["【部分对话总结】本次练习。\n【全部对话总结】本次练习。"]
        )
        request = {
            "request_id": "summary",
            "user": "doctor",
            "query": "整理本次问诊",
            "response_mode": "blocking",
        }
        assert client.post("/v1/chat", json=request).status_code == 422
        for role in ("student", "patient", "operator"):
            denied = client.post("/v1/chat", json={**request, "inputs": {"role": role}})
            assert denied.status_code == 403
        inputs = {"tenant_id": "tenant", "role": "clinician", "consultation_id": "visit-a"}
        for extra, code in [
            ({}, "resource_denied"),
            ({"pos": "大厅", "resource_context_token": "proof-a"}, "consultation_required"),
        ]:
            result = client.post(
                "/v1/chat", json={**request, "request_id": code, "inputs": {**inputs, **extra}}
            ).json()
            assert any(
                e["type"] == "task_failed" and e["payload"]["code"] == code
                for e in result["events"]
            )
        assert not model.seen
        inputs.update(
            resource_context_token="proof-a", records=[{"part": "仅供本次问诊的练习记录"}]
        )
        result = client.post("/v1/chat", json={**request, "inputs": inputs}).json()
        assert result["status"] == "succeeded"
        assert result["metrics"]["branch"] == "ai_summary"
        assert result["metrics"]["agent_id"] == "clinician_workspace"
        assert result["metrics"]["summary_source"] == "inputs.records"
        assert any("仅供本次问诊的练习记录" in prompt for prompt in model.seen)


def test_function_tool_list_restricts_both_model_and_gateway():
    async def check():
        async with runtime() as (container, backend):
            client = ClientIdentity("custom", allowed_tools=frozenset({"get_patient_profile"}))
            scope = replace(
                patient_scope("tenant", "actor"),
                client_id=client.name,
                allowed_tools=client.allowed_tools,
            )
            ctx = context(container, "查看本人档案", scope=scope)
            ctx.client = client
            assert {tool.name for tool in available_tools(ctx, build_tools())} == {
                "get_patient_profile"
            }
            await ctx.call("get_patient_profile", {})
            with pytest.raises(DomainError, match="不允许") as denied:
                await ctx.call("get_irego_patient_history", {})
            assert denied.value.code == "client_denied"
            assert [name for name, _ in backend.calls] == ["get_patient_profile"]

    asyncio.run(check())


def test_frontend_service_token_requires_role_even_without_a_fixed_flow(settings):
    settings.service_clients = [ClientTokenConfig(name="campus", token="test-campus-client")]
    with TestClient(
        create_app(settings), headers={"Authorization": "Bearer test-campus-client"}
    ) as client:
        query = {
            "user": "student1",
            "query": "帮我复习",
            "request_id": "study",
            "response_mode": "blocking",
        }
        assert client.post("/v1/chat", json=query).status_code == 422
        result = client.post("/v1/chat", json={**query, "inputs": {"identity": "学生"}}).json()
        assert result["metrics"]["agent_id"] == "student_tutor"
        assert result["metrics"]["branch"] == "default"
