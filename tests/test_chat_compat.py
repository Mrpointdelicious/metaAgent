"""
创建日期：2026-10-26
文件功能：验证原生及交互接口共存、流式生命周期和未修改脚本的真实 HTTP 连通性。
"""

import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn

from meta_agent.api.routes import chat, dify_stream
from meta_agent.api.schemas import DifyChatRequest, NativeChatRequest
from meta_agent.app import create_app
from meta_agent.contracts import OutboundEvent, RunRecord
from meta_agent.events.dify import DifyEventAdapter
from tests.helpers.runtime import request, runtime

ENDPOINTS = ["/v1/chat", "/v1/chat-messages", "/compat/dify/v1/chat-messages"]


def payload(**changes):
    return {
        "user": "interactive-cli",
        "inputs": {},
        "query": "你好",
        "conversation_id": "",
        "response_mode": "blocking",
        **changes,
    }


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_compat_blocking_assigns_unique_ids_and_reuses_conversation(client, auth_headers, endpoint):
    first = client.post(endpoint, headers=auth_headers, json=payload())
    assert first.status_code == 200
    body = first.json()
    assert body["event"] == "message" and body["answer"]
    assert body["metadata"]["status"] == "succeeded"
    assert body["message_id"] == body["id"]
    second = client.post(
        endpoint,
        headers=auth_headers,
        json=payload(query="谢谢", conversation_id=body["conversation_id"]),
    ).json()
    assert second["task_id"] != body["task_id"]
    assert second["message_id"] != body["message_id"]
    assert second["conversation_id"] == body["conversation_id"]
    third = client.post(endpoint, headers=auth_headers, json=payload()).json()
    assert third["conversation_id"] != body["conversation_id"]
    status = client.post(
        f"/v1/runs/{body['task_id']}/status",
        headers=auth_headers,
        json={"user": "interactive-cli", "inputs": {}},
    )
    assert status.status_code == 200 and status.json()["run_id"] == body["task_id"]
    other = client.post(
        f"/v1/runs/{body['task_id']}/status",
        headers=auth_headers,
        json={"user": "other", "inputs": {}},
    )
    assert other.status_code == 404


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_compat_streaming_emits_settled_text_and_message_end(client, auth_headers, endpoint):
    response = client.post(endpoint, headers=auth_headers, json=payload(response_mode="streaming"))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert [frame["event"] for frame in frames] == ["message", "message_end"]
    assert frames[0]["answer"] and "answer" not in frames[1]
    assert frames[0]["task_id"] == frames[1]["task_id"] == response.headers["x-run-id"]
    assert frames[0]["message_id"] == frames[1]["message_id"]
    assert frames[1]["metadata"]["status"] == "succeeded"
    assert "OutboundEvent" not in response.text


@pytest.mark.parametrize("request_id", [None, "", " "])
def test_invalid_native_request_id_cannot_fall_back_to_compat(client, auth_headers, request_id):
    assert (
        client.post(
            "/v1/chat", headers=auth_headers, json=payload(request_id=request_id)
        ).status_code
        == 422
    )


def test_native_extra_fields_remain_rejected_and_native_shape_is_preserved(client, auth_headers):
    request_body = payload(request_id="native")
    assert (
        client.post(
            "/v1/chat", headers=auth_headers, json={**request_body, "files": []}
        ).status_code
        == 422
    )
    body = client.post("/v1/chat", headers=auth_headers, json=request_body).json()
    assert body["status"] == "succeeded" and body["events"]
    assert "answer" not in body and "task_id" not in body


def test_alias_explicit_id_keeps_idempotency_without_replaying_a_stream(client, auth_headers):
    request_body = payload(request_id="deduplicated")
    first = client.post("/v1/chat-messages", headers=auth_headers, json=request_body).json()
    again = client.post(
        "/v1/chat-messages",
        headers=auth_headers,
        json={**request_body, "response_mode": "streaming"},
    )
    assert again.headers["content-type"].startswith("application/json")
    assert again.json()["task_id"] == first["task_id"]
    assert again.json()["metadata"]["reused"] is True
    assert (
        client.post(
            "/v1/chat-messages", headers=auth_headers, json={**request_body, "query": "别的问题"}
        ).status_code
        == 409
    )


@pytest.mark.parametrize("phone_key", ["patientphone", "patientPhone", "patient_phone"])
def test_phone_alias_is_resolved_for_chat_and_followup_status(
    client, auth_headers, monkeypatch, phone_key
):
    calls = []

    async def resolve(**kwargs):
        calls.append(kwargs)
        return {"project_patient_id": 3799, "robot_patient_id": 461}

    monkeypatch.setattr(
        client.app.state.container.ai_webapi_client, "resolve_patient_identity", resolve
    )
    inputs = {phone_key: "13800000000"}
    response = client.post("/v1/chat", headers=auth_headers, json=payload(inputs=inputs))
    assert response.status_code == 200
    body = response.json()
    assert calls == [{"project_patient_id": None, "phone": "13800000000"}]
    status = client.post(
        f"/v1/runs/{body['task_id']}/status",
        headers=auth_headers,
        json={"user": "interactive-cli", "inputs": inputs},
    )
    assert status.status_code == 200
    assert len(calls) == 2


def test_conflicting_phone_aliases_are_rejected(client, auth_headers):
    response = client.post(
        "/v1/chat",
        headers=auth_headers,
        json=payload(inputs={"patientphone": "13800000000", "patientPhone": "13900000000"}),
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "changes",
    [
        {"query": " "},
        {"user": " "},
        {"files": [{"upload_file_id": "unsupported"}]},
        {"query": "x" * 4001},
    ],
)
def test_compat_validates_text_identity_and_unsupported_files(client, auth_headers, changes):
    assert client.post("/v1/chat", headers=auth_headers, json=payload(**changes)).status_code == 422


def test_openapi_describes_both_chat_protocols_and_aliases(client):
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    assert "request_id" in schemas["NativeChatRequest"]["required"]
    assert "request_id" not in schemas["DifyChatRequest"]["required"]
    assert "/v1/chat-messages" in spec["paths"]


def test_failed_run_has_an_error_frame_and_a_terminal_status(client, auth_headers, monkeypatch):
    async def fail(*args, **kwargs):
        from meta_agent.contracts import DomainError

        raise DomainError("synthetic_failure", "合成验证失败。")

    monkeypatch.setattr(client.app.state.container.application.planner, "parse", fail)
    response = client.post(
        "/v1/chat", headers=auth_headers, json=payload(response_mode="streaming")
    )
    frames = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert [frame["event"] for frame in frames] == ["error", "message_end"]
    assert frames[0]["code"] == "synthetic_failure"
    assert "合成验证失败" in frames[0]["message"]
    assert frames[-1]["metadata"]["status"] == "failed"


def test_adapter_replaces_revisions_preserves_gaps_and_does_not_dispatch_actions():
    record = RunRecord(
        run_id="run_sample",
        request_id="request",
        request_hash="hash",
        scope_key="scope",
        conversation_id="conversation",
        status="partial",
        goal_statuses={"a": "succeeded", "b": "failed"},
        action_delivery={"action": "delivery_unknown"},
    )
    for kind, goal, body in [
        ("answer_part", "a", {"text": "old", "revision": 1}),
        ("answer_part", "a", {"text": "final", "revision": 2}),
        ("task_failed", "b", {"text": "另一项暂不可用", "code": "unavailable", "retryable": False}),
        (
            "artifact_ready",
            "a",
            {
                "artifact_ref": "image",
                "url": "https://images.test/report.png",
                "evidence_id": "e",
                "expires_at": record.created_at,
            },
        ),
    ]:
        record.events.append(
            OutboundEvent(
                request_id=record.request_id,
                run_id=record.run_id,
                conversation_id=record.conversation_id,
                seq=len(record.events) + 1,
                goal_id=goal,
                type=kind,
                payload=body,
            )
        )
    adapter = DifyEventAdapter()
    body = adapter.message(record)
    assert "final" in body["answer"] and "old" not in body["answer"]
    assert "另一项暂不可用" in body["answer"]
    assert "![报告图片](https://images.test/report.png)" in body["answer"]
    assert body["metadata"]["action_delivery"] == {"action": "delivery_unknown"}
    assert record.action_delivery == {"action": "delivery_unknown"}
    assert isinstance(body["metadata"]["artifacts"][0]["expires_at"], str)
    record.status = "failed"
    frames = adapter.frames(record)
    assert frames[0]["event"] == "error" and frames[0]["code"] == "unavailable"
    assert frames[-1]["event"] == "message_end"


def test_adapter_respects_explicit_replacement_without_goal_ids():
    record = RunRecord(
        run_id="run_sample",
        request_id="request",
        request_hash="hash",
        scope_key="scope",
        conversation_id="conversation",
        status="succeeded",
    )
    for seq, event_id, body in [
        (1, "old", {"text": "旧回答"}),
        (2, "new", {"text": "替换后的回答", "revision": 2, "replaces": "old"}),
    ]:
        record.events.append(
            OutboundEvent(
                event_id=event_id,
                request_id=record.request_id,
                run_id=record.run_id,
                conversation_id=record.conversation_id,
                seq=seq,
                type="answer_part",
                payload=body,
            )
        )
    assert DifyEventAdapter().message(record)["answer"] == "替换后的回答"


def test_compat_stream_disconnect_cancels_the_original_application_run():
    async def check():
        async with runtime() as (container, backend):
            backend.gates["get_irego_session_analysis"] = asyncio.Event()
            run = await container.application.start(request("解读最近训练"))
            fake_request = SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace(container=container))
            )
            generator = dify_stream(run, fake_request)
            pending = asyncio.create_task(anext(generator))
            async with asyncio.timeout(3):
                while not any(name == "get_irego_session_analysis" for name, _ in backend.calls):
                    await asyncio.sleep(0.001)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert run.record.status == "cancelled"
            assert run.emitter.closed
            assert "get_irego_session_analysis" in backend.cancelled

    asyncio.run(check())


@pytest.mark.parametrize("model", [NativeChatRequest, DifyChatRequest])
def test_blocking_request_cancellation_stops_the_application(model):
    async def check():
        async with runtime() as (container, backend):
            backend.gates["get_irego_session_analysis"] = asyncio.Event()
            fake_request = SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace(container=container))
            )
            pending = asyncio.create_task(
                chat(
                    model(
                        query="解读最近训练",
                        request_id="cancel",
                        user="actor",
                        inputs={"patientId": "461"},
                        response_mode="blocking",
                    ),
                    fake_request,
                )
            )
            async with asyncio.timeout(3):
                while not any(name == "get_irego_session_analysis" for name, _ in backend.calls):
                    await asyncio.sleep(0.001)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert "get_irego_session_analysis" in backend.cancelled
            assert not container.application.active

    asyncio.run(check())


@pytest.mark.parametrize("identity", ["3799", "phone:13800000000"])
def test_unmodified_interactive_script_can_chat_and_fetch_verbose_status(
    settings, monkeypatch, identity
):
    async def resolve(self, **kwargs):
        return {"project_patient_id": 3799, "robot_patient_id": 461}

    monkeypatch.setattr(
        "meta_agent.tools.ai_webapi.AIWebApiClient.resolve_patient_identity", resolve
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), log_config=None, access_log=False, lifespan="on")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parents[1] / "interactive.py"),
                f"http://127.0.0.1:{sock.getsockname()[1]}",
                "-i",
                identity,
                "-t",
                "test-service-token",
                "-v",
            ],
            input="你好\n谢谢\nexit\n",
            capture_output=True,
            encoding="utf-8",
            timeout=20,
            check=True,
        )
        assert result.stdout.count("答 > ") == 2
        assert result.stdout.count("状态: succeeded") == 2
        assert "[错误]" not in result.stdout and "[无回答]" not in result.stdout
        assert "[指标不可用]" not in result.stdout and "[无 task_id" not in result.stdout
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
    assert not thread.is_alive()
