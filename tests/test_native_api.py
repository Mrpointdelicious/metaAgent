"""
创建日期：2026-09-12
文件功能：验证原生事件、请求幂等、鉴权隔离以及未启用的 Dify 框架。
"""

import json

import pytest

from meta_agent.contracts import OutboundEvent


def payload(query="解读最近训练", request_id="req-1", **kwargs):
    return {
        "query": query,
        "request_id": request_id,
        "user": "actor",
        "conversation_id": "conversation",
        "inputs": {"patientId": "461"},
        "response_mode": "blocking",
        **kwargs,
    }


def test_blocking_native_facts_and_idempotency(client, auth_headers):
    request = payload()
    response = client.post("/v1/chat", headers=auth_headers, json=request)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    events = [OutboundEvent.model_validate(e) for e in body["events"]]
    assert events[0].type == "accepted" and events[-1].type == "completed"
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    answer = [e for e in events if e.type == "answer_part"][-1]
    assert "0.5 m/s" in answer.payload["text"] and answer.payload["fact_ids"]
    again = client.post("/v1/chat", headers=auth_headers, json=request).json()
    assert again["run_id"] == body["run_id"] and again["reused"]
    conflict = client.post("/v1/chat", headers=auth_headers, json={**request, "query": "你好"})
    assert conflict.status_code == 409


def test_streaming_native_sequence(client, auth_headers):
    response = client.post(
        "/v1/chat", headers=auth_headers, json=payload("你好", response_mode="streaming", inputs={})
    )
    assert response.status_code == 200
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert events[0]["type"] == "accepted"
    assert events[-1]["type"] == "completed"
    assert events[-1]["payload"]["task_statuses"] == {}


def test_scope_protects_status_and_cancel(client, auth_headers):
    own = client.post("/v1/chat", headers=auth_headers, json=payload()).json()
    access = {"user": "actor", "inputs": {"patientId": "461"}}
    path = f"/v1/runs/{own['run_id']}"
    assert client.post(path + "/status", headers=auth_headers, json=access).status_code == 200
    for changed in (
        {"patientId": "462"},
        {"patientId": "461", "role": "clinician"},
        {"patientId": "461", "tenantId": "another"},
    ):
        for endpoint in ("/status", "/cancel"):
            assert (
                client.post(
                    path + endpoint, headers=auth_headers, json={**access, "inputs": changed}
                ).status_code
                == 404
            )
    other = client.post(
        "/v1/chat", headers=auth_headers, json=payload(inputs={"patientId": "462"})
    ).json()
    assert other["run_id"] != own["run_id"]


@pytest.mark.parametrize(
    "endpoint", ["/v1/chat", "/compat/dify/v1/chat-messages", "/compat/dify/v1/workflows/run"]
)
def test_authentication(client, endpoint):
    assert client.post(endpoint, json=payload()).status_code == 401


@pytest.mark.parametrize(
    "endpoint", ["/compat/dify/v1/chat-messages", "/compat/dify/v1/workflows/run"]
)
def test_dify_is_explicitly_deferred(client, auth_headers, endpoint):
    assert client.post(endpoint, headers=auth_headers, json=payload()).status_code == 501


def test_optional_patient_and_mixed_identity(client, auth_headers):
    response = client.post(
        "/v1/chat",
        headers=auth_headers,
        json=payload("查询在线医生", inputs={"spaceId": "demo-space"}),
    )
    assert response.json()["status"] == "succeeded"
    assert response.json()["metrics"]["tool_calls"] == 1
    response = client.post(
        "/v1/chat",
        headers=auth_headers,
        json=payload(
            "查一下患者信息", "req-2", inputs={"patientId": {"type": "mixed", "value": "461"}}
        ),
    )
    assert response.json()["status"] == "succeeded"
