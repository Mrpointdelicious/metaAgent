"""
创建日期：2026-08-29
文件功能：验证 Dify 风格阻塞、SSE 流式响应及旧标签输出顺序。
"""

from fastapi.testclient import TestClient


def _payload(response_mode: str) -> dict[str, object]:
    return {
        "inputs": {"patientId": "461"},
        "query": "解读最近训练并生成报告图片",
        "response_mode": response_mode,
        "conversation_id": "conversation-test",
        "user": "user-test",
    }


def test_blocking_chat_response_uses_legacy_tags(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    response = client.post(
        "/compat/dify/v1/chat-messages",
        headers=auth_headers,
        json=_payload("blocking"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["event"] == "message"
    assert "[mode]rehab_report" in body["answer"]
    assert "[answer]" in body["answer"]
    assert "[image]" in body["answer"]
    assert body["metadata"]["result_ref"].startswith("result_")


def test_streaming_chat_response_has_dify_events(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    response = client.post(
        "/compat/dify/v1/chat-messages",
        headers=auth_headers,
        json=_payload("streaming"),
    )

    assert response.status_code == 200
    content = response.text
    assert "event: ping" in content
    assert '"event":"workflow_started"' in content
    assert '"event":"message"' in content
    assert "[answer]" in content
    assert "[image]" in content
    assert '"event":"workflow_finished"' in content


def test_service_authentication_is_required(client: TestClient) -> None:
    response = client.post(
        "/compat/dify/v1/chat-messages",
        json=_payload("blocking"),
    )
    assert response.status_code == 401
