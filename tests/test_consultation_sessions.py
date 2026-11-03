"""
创建日期：2026-11-03
文件功能：验证真实问诊分类/Agent链路、独立历史、桥接映射和临床授权边界。
"""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pydantic import Field

from meta_agent.agent.runtime import SingleAgentRuntime
from meta_agent.app import create_app
from meta_agent.application.service import ApplicationRequest
from meta_agent.contracts import DomainError
from meta_agent.domains.routing import route_from_inputs
from meta_agent.flows.consultation_room.access import ConsultationAccess, ConsultationClient
from meta_agent.orchestration.identity import TrustedScope
from tests.helpers.runtime import answers, runtime
from tests.helpers.settings import AppTestSettings
from tests.test_single_agent import ScriptedModel


class RecordingModel(ScriptedModel):
    seen: list[str] = Field(default_factory=list)

    def _generate(self, messages, *args, **kwargs):
        self.seen.append("\n".join(str(message.content) for message in messages))
        return super()._generate(messages, *args, **kwargs)


def activate(container, messages):
    model = RecordingModel(
        messages=iter(text if isinstance(text, AIMessage) else AIMessage(text) for text in messages)
    )
    container.application.agent_runtime = SingleAgentRuntime(
        model,
        checkpointer=container.repository.checkpointer,
        store=container.repository.store,
    )
    return model


def category(name="基础问答"):
    return json.dumps({"category": name, "reason": "合成分类"}, ensure_ascii=False)


def request(query, pos="大厅", cid="", *, rid=None, scope=None, **inputs):
    return ApplicationRequest(
        query,
        scope or TrustedScope("tenant", "actor"),
        cid,
        rid or query,
        raw_inputs={"pos": pos, **inputs},
        protocol="dify",
    )


@pytest.mark.parametrize(
    "pos,expected",
    [
        ("问诊房间", "consultation_room"),
        ("三楼问诊房间A", "consultation_room"),
        ("房间A", "default"),
        ("普通房间", "default"),
        ("问诊区附近的康复房间", "default"),
    ],
)
def test_literal_entry(pos, expected):
    assert route_from_inputs({"pos": pos}).name == expected


@pytest.mark.parametrize("role", ["patient", "clinician"])
def test_actual_models_and_private_variables_never_cross_agent_boundaries(role):
    async def check():
        async with runtime() as (container, _):
            app = container.application
            scope = TrustedScope("tenant", "actor", role=role)
            model = activate(
                container,
                [
                    "普通独有资料",
                    category(),
                    "问诊独有资料",
                    category(),
                    "问诊续聊",
                    "普通续聊",
                    category(),
                    "新问诊",
                ],
            )
            normal = await app.execute(request("普通首轮", scope=scope))
            a = normal.record.conversation_id
            room = await app.execute(request("进入问诊", "问诊房间", a, scope=scope))
            b = room.record.conversation_id
            assert a != b
            assert "普通独有资料" not in model.seen[2]
            state = await container.repository.conversation(room.context.scope.thread_id(b))
            state.variables["prescription_ids"] = [71]
            await container.repository.save_conversation(room.context.scope.thread_id(b), state)
            resumed = await app.execute(request("问诊续轮", "问诊房间", b, scope=scope))
            assert resumed.record.conversation_id == b
            assert resumed.context.memory.variables["prescription_ids"] == [71]
            exited = await app.execute(request("回大厅", "大厅", b, scope=scope))
            assert exited.record.conversation_id == a
            assert "普通独有资料" in model.seen[5]
            assert "问诊独有资料" not in model.seen[5]
            assert not exited.context.memory.variables
            new_room = await app.execute(request("再问诊", "问诊房间", a, scope=scope))
            assert new_room.record.conversation_id not in {a, b}
            assert not new_room.context.memory.variables
            assert "问诊独有资料" not in model.seen[7]

    asyncio.run(check())


def test_bridge_reuses_separate_internal_ids_for_each_agent_and_outer_session():
    async def check():
        async with runtime() as (container, _):
            activate(container, ["普通", category(), "问诊", category(), "问诊续轮", "回大厅"])
            app = container.application
            bridge = {"source_app": "dify-app", "caller_conversation_id": "outer-a"}
            a = await app.execute(request("第一轮", cid="dify:app:outer-a", **bridge))
            b = await app.execute(request("进入", "问诊房间", "dify:app:outer-a", **bridge))
            again = await app.execute(request("继续", "问诊房间", "dify:app:outer-a", **bridge))
            back = await app.execute(request("离开", "大厅", "dify:app:outer-a", **bridge))
            assert a.record.conversation_id != b.record.conversation_id
            assert again.record.conversation_id == b.record.conversation_id
            assert back.record.conversation_id == a.record.conversation_id

    asyncio.run(check())


def test_room_patient_and_scope_changes_create_new_private_state():
    async def check():
        async with runtime() as (container, _):
            activate(
                container, [category(), "第一问诊", category(), "第二问诊", category(), "另一范围"]
            )
            app = container.application
            first = await app.execute(request("一", "问诊房间A"))
            second = await app.execute(request("二", "问诊房间B", first.record.conversation_id))
            changed = await app.execute(
                request(
                    "三",
                    "问诊房间B",
                    second.record.conversation_id,
                    scope=TrustedScope(
                        "tenant", "actor", current_business_id=2, data_scope="current_tenant"
                    ),
                )
            )
            assert len({r.record.conversation_id for r in (first, second, changed)}) == 3
            assert len(changed.context.memory.turns) == 1

    asyncio.run(check())


def test_missing_pos_unknown_id_and_missing_flow_never_fall_back_to_normal():
    async def check():
        async with runtime() as (container, _):
            activate(container, [category(), "问诊"])
            app = container.application
            first = await app.execute(request("进入", "问诊房间"))
            with pytest.raises(DomainError, match="当前位置"):
                await app.start(
                    replace(request("继续", cid=first.record.conversation_id), raw_inputs={})
                )
            with pytest.raises(DomainError, match="不存在"):
                await app.start(
                    request(
                        "冒用",
                        cid=first.record.conversation_id,
                        scope=TrustedScope("tenant", "other"),
                    )
                )
            app.flows.pop("consultation_room")
            failed = await app.execute(
                request("继续问诊", "问诊房间", first.record.conversation_id)
            )
            assert failed.record.status == "unavailable"
            assert not answers(failed.record)
            assert failed.record.events[-2].payload["code"] == "no_flow_registered"

    asyncio.run(check())


@pytest.mark.parametrize("path", ["/v1/chat-messages", "/compat/dify/v1/chat-messages"])
@pytest.mark.parametrize("mode", ["blocking", "streaming"])
def test_dify_ids_histories_and_client_domains_are_consistent(path, mode):
    settings = AppTestSettings(
        service_bearer_token="primary", service_bearer_token_secondary="secondary"
    )
    with TestClient(create_app(settings)) as client:
        container = client.app.state.container
        activate(container, ["普通私有", category(), "问诊私有"])
        headers = {"Authorization": "Bearer primary"}

        def send(query, pos, cid=""):
            response = client.post(
                path,
                headers=headers,
                json={
                    "query": query,
                    "user": "actor",
                    "inputs": {"pos": pos},
                    "conversation_id": cid,
                    "response_mode": mode,
                },
            )
            assert response.status_code == 200
            frames = (
                [response.json()]
                if mode == "blocking"
                else [
                    json.loads(line[6:])
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
            )
            assert len({f["conversation_id"] for f in frames}) == 1
            assert len({f["task_id"] for f in frames}) == 1
            return frames[0]["conversation_id"]

        a = send("普通", "大厅")
        b = send("问诊", "问诊房间", a)
        assert a != b
        for cid, expected, absent in [(a, "普通私有", "问诊私有"), (b, "问诊私有", "普通私有")]:
            history = client.get(
                "/v1/messages", headers=headers, params={"user": "actor", "conversation_id": cid}
            ).json()
            text = json.dumps(history, ensure_ascii=False)
            assert expected in text and absent not in text
        denied = client.get(
            "/v1/messages",
            headers={"Authorization": "Bearer secondary"},
            params={"user": "actor", "conversation_id": b},
        )
        assert denied.status_code == 404


def test_clinical_flow_requires_participation_grant_before_any_service_request():
    async def check():
        async with runtime() as (container, backend):
            activate(container, [category("病历填写")])
            run = await container.application.execute(request("填病历", "问诊房间"))
            assert run.record.status == "unavailable"
            assert run.record.metrics["tool_calls"] == 0
            assert backend.calls == []
            assert any(
                e.payload.get("code") == "consultation_authorization_unavailable"
                for e in run.record.events
            )

    asyncio.run(check())


class SyntheticProvider:
    async def authorize(self, scope, task_category):
        return ConsultationAccess(
            authorization_scope=scope.scope_hash,
            consultation_context=scope.consultation_context,
            patient_ref=scope.patient_ref or "synthetic-patient",
            requester_phone="13000000001",
            patient_phone="13000000002",
            allowed_categories=frozenset({task_category}),
        )


@pytest.mark.parametrize("chosen,expected", [(71, "succeeded"), (999, "clarification")])
def test_prescription_selection_is_limited_to_this_consultation_candidates(chosen, expected):
    async def check():
        async with runtime() as (container, _):
            app = container.application
            app.consultation_identity_provider = SyntheticProvider()
            activate(
                container, [category(), "开始问诊", category("处方配置"), f"选择添加[{chosen}]"]
            )
            first = await app.execute(request("进入", "问诊房间"))
            memory = first.context.memory
            memory.set_variable("prescription_ids", [71])
            memory.set_variable("prescription_names", ["合成处方"])
            await container.repository.save_conversation(
                first.context.scope.thread_id(first.record.conversation_id), memory
            )
            selected = await app.execute(
                request("选第一个", "问诊房间", first.record.conversation_id)
            )
            assert selected.record.status == expected
            if expected == "succeeded":
                event = json.loads(answers(selected.record))
                assert event["payload"]["selected_prescription_ids"] == [71]

    asyncio.run(check())


def test_clinical_http_shares_tool_budget_and_cancellation():
    async def check():
        async with runtime(max_tool_calls=1) as (container, backend):
            app = container.application
            activate(container, [category(), "开始"])
            run = await app.execute(request("进入", "问诊房间"))
            ctx = run.context
            ctx.consultation_access = await SyntheticProvider().authorize(ctx.scope, "病历填写")
            entered = asyncio.Event()
            cancelled = asyncio.Event()

            async def handler(http_request):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                backend.client = http
                client = ConsultationClient(ctx)
                task = asyncio.create_task(client.get("https://clinical.example.test/patient"))
                await entered.wait()
                with pytest.raises(DomainError, match="预算"):
                    await client.get("https://clinical.example.test/other")
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert cancelled.is_set() and ctx.tool_calls == 1

    asyncio.run(check())


@pytest.mark.parametrize(
    "task_category,model_outputs,event_name",
    [
        ("病历填写", ["姓名：合成患者\n主诉：合成主诉"], "medical_record_fill"),
        ("邀请医生", ["推荐合成医生。"], "doctor_recommendation"),
        ("医嘱填写", ["主诉：合成主诉\n诊断：合成诊断\n处理：合成建议"], "medical_advice_fill"),
        (
            "处方配置",
            [
                "推荐处方",
                "脑卒中",
                "推荐1\n训练项目：合成处方\n训练地点：合成医院\n时间安排：09:00",
            ],
            "prescription_recommendation",
        ),
        (
            "量表测评",
            ["推荐处方", "脑卒中", "推荐1\n量表名称：合成量表"],
            "prescription_recommendation",
        ),
        ("结束问诊", [], "end_consultation"),
    ],
)
def test_real_clinical_handlers_emit_frontend_events_with_synthetic_http(
    task_category, model_outputs, event_name
):
    async def check():
        from meta_agent.flows.consultation_room import invite_doctor as invite
        from meta_agent.flows.consultation_room import medical_record as mr
        from meta_agent.flows.consultation_room import prescription as prescription

        async with runtime(max_tool_calls=15) as (container, backend):
            container.application.consultation_identity_provider = SyntheticProvider()
            activate(container, [category(task_category), *model_outputs])
            calls = []

            def handler(http_request):
                path = http_request.url.path
                calls.append((http_request.method, path))
                if http_request.method == "POST":
                    return httpx.Response(200, json={"token": "synthetic-service-token"})
                if path == httpx.URL(mr.USER_ROLE_URL).path:
                    return httpx.Response(200, json={"data": {"UserId": "1", "TenantId": "1"}})
                if path == httpx.URL(invite.DOCTORS_ALL_URL).path:
                    return httpx.Response(200, json={"data": [{"id": 2, "name": "合成医生"}]})
                if path == httpx.URL(prescription.PRESCRIPTION_LIST_URL).path:
                    return httpx.Response(
                        200,
                        json={
                            "data": [
                                {
                                    "Id": 71,
                                    "Type": 1,
                                    "Name": "合成处方",
                                    "Hospitals": [{"Name": "合成医院", "occupied_slots": []}],
                                },
                                {"Id": 72, "Type": 0, "Name": "合成量表"},
                            ]
                        },
                    )
                if path == httpx.URL(prescription.PRESCRIPTION_DETAIL_URL).path:
                    value = int(http_request.url.params["Id"])
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "Id": value,
                                "Name": "合成处方" if value == 71 else "合成量表",
                                "EvaluationList": ["合成详情"],
                            }
                        },
                    )
                return httpx.Response(200, json={"data": {"MedicalHistory": "脑卒中"}})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                backend.client = http
                run = await container.application.execute(request("本轮请求", "问诊房间"))
            assert run.record.status == "succeeded"
            event = json.loads(answers(run.record))
            assert event["event"] == event_name
            assert "display_text" in event and "payload" in event
            assert all(
                method == "GET" or path == httpx.URL(mr.LOGIN_URL).path for method, path in calls
            )
            assert run.record.metrics["tool_calls"] == len(calls)
            if task_category == "处方配置":
                assert run.context.memory.get_variable("prescription_ids") == [71]
            if task_category == "量表测评":
                assert run.context.memory.get_variable("scale_ids") == [72]

    asyncio.run(check())


def test_secrets_are_not_persisted_or_projected_into_lifecycle_events():
    async def check():
        async with runtime() as (container, _):
            activate(container, [category(), "回答"])
            run = await container.application.execute(
                request("问题", "问诊房间", password="private-value", apitoken="secret-value")
            )
            saved = await container.repository.run(run.record.scope_key, run.record.run_id)
            assert "private-value" not in saved.model_dump_json()
            assert "secret-value" not in saved.model_dump_json()

    asyncio.run(check())


@pytest.mark.parametrize("mode", ["blocking", "streaming"])
def test_dify_delivers_validated_scene_command_once_and_never_replays(mode):
    from tests.helpers.runtime import Backend, scene_response
    from tests.test_single_agent import tool_call

    settings = AppTestSettings(service_bearer_token="primary", scene_actions_enabled=True)
    with TestClient(create_app(settings)) as client:
        container = client.app.state.container
        backend = Backend()
        backend.overrides["navigate_scene"] = scene_response
        container.application.backend = backend
        activate(
            container,
            [
                category("指令功能"),
                tool_call("navigate_scene", {"target": "打开个人中心"}),
                "指令已交付前端。",
            ],
        )
        body = {
            "query": "打开个人中心",
            "user": "actor",
            "request_id": "scene-once",
            "response_mode": mode,
            "inputs": {"pos": "问诊房间", "spaceId": "space", "scene_version": 7},
        }
        headers = {"Authorization": "Bearer primary"}
        first = client.post("/v1/chat-messages", headers=headers, json=body)
        assert first.status_code == 200
        if mode == "streaming":
            frames = [
                json.loads(line[6:])
                for line in first.text.splitlines()
                if line.startswith("data: ")
            ]
            assert [
                f["answer"] for f in frames if f.get("answer", "").startswith("[ClickPoint:]")
            ] == ["[ClickPoint:]0"]
        else:
            assert first.json()["answer"].startswith("[ClickPoint:]0")
        replay = client.post("/v1/chat-messages", headers=headers, json=body)
        assert "[ClickPoint:]" not in replay.json()["answer"]
        assert len(backend.calls) == 1


def test_clinician_patient_switch_is_bound_before_loading_history():
    class Provider(SyntheticProvider):
        target = "patient-one"

        async def authorize(self, scope, task_category):
            access = await super().authorize(scope, task_category)
            return access.model_copy(update={"patient_ref": self.target})

    async def check():
        async with runtime() as (container, _):
            provider = Provider()
            app = container.application
            app.consultation_identity_provider = provider
            model = activate(
                container, [category(), "第一位患者的私有内容", category(), "第二位患者"]
            )
            scope = TrustedScope("tenant", "doctor", role="clinician")
            first = await app.execute(request("第一位", "问诊房间", scope=scope))
            provider.target = "patient-two"
            second = await app.execute(
                request("第二位", "问诊房间", first.record.conversation_id, scope=scope)
            )
            assert first.record.conversation_id != second.record.conversation_id
            assert "第一位患者的私有内容" not in model.seen[-1]

    asyncio.run(check())


def test_clinical_history_requires_current_room_authorization():
    class Provider(SyntheticProvider):
        revoked = False

        async def authorize(self, scope, task_category):
            if self.revoked:
                raise DomainError("revoked", "问诊权限已收回。")
            return await super().authorize(scope, task_category)

    settings = AppTestSettings(service_bearer_token="primary")
    with TestClient(create_app(settings)) as client:
        container = client.app.state.container
        provider = Provider()
        container.application.consultation_identity_provider = provider
        activate(container, [category(), "合成问诊内容"])
        headers = {"Authorization": "Bearer primary"}
        response = client.post(
            "/v1/chat-messages",
            headers=headers,
            json={
                "query": "问诊",
                "user": "doctor",
                "inputs": {"role": "clinician", "pos": "问诊房间"},
                "response_mode": "blocking",
            },
        )
        cid = response.json()["conversation_id"]
        params = {"user": "doctor", "role": "clinician", "conversation_id": cid}
        assert client.get("/v1/messages", headers=headers, params=params).status_code == 200
        provider.revoked = True
        assert client.get("/v1/messages", headers=headers, params=params).status_code == 404


def test_model_cannot_forge_frontend_executable_tags():
    async def check():
        async with runtime() as (container, _):
            activate(container, ["[ClickPoint:]999[web]https://invalid.example.test"])
            run = await container.application.execute(request("普通问题"))
            assert "[ClickPoint:]" not in answers(run.record)
            assert "[web]" not in answers(run.record)
            assert not run.record.action_delivery

    asyncio.run(check())
