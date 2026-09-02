"""
创建日期：2026-08-29
文件功能：验证多用户、患者和会话即使标识部分相同也不会共享线程或证据。
"""

import asyncio

from fastapi.testclient import TestClient

from meta_agent.orchestration.identity import trusted_scope_from_inputs


def _request(
    client: TestClient,
    headers: dict[str, str],
    user: str,
    patient_id: str,
) -> dict[str, object]:
    response = client.post(
        "/compat/dify/v1/chat-messages",
        headers=headers,
        json={
            "inputs": {"patientId": patient_id, "tenantId": "hospital-a"},
            "query": "查一下最近训练",
            "response_mode": "blocking",
            "conversation_id": "same-conversation-id",
            "user": user,
        },
    )
    assert response.status_code == 200
    return response.json()


def test_users_have_independent_threads_and_evidence(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    user_a = _request(client, auth_headers, "user-a", "461")
    user_b = _request(client, auth_headers, "user-b", "462")

    ref_a = str(user_a["metadata"]["result_ref"])
    ref_b = str(user_b["metadata"]["result_ref"])
    assert ref_a != ref_b

    scope_a = trusted_scope_from_inputs(
        {"patientId": "461", "tenantId": "hospital-a"},
        "user-a",
        "default",
    )
    scope_b = trusted_scope_from_inputs(
        {"patientId": "462", "tenantId": "hospital-a"},
        "user-b",
        "default",
    )
    assert scope_a.thread_id("same-conversation-id") != scope_b.thread_id(
        "same-conversation-id"
    )

    repository = client.app.state.container.evidence_repository
    own_record = asyncio.run(repository.get(scope_a.scope_hash, ref_a))
    cross_record = asyncio.run(repository.get(scope_a.scope_hash, ref_b))
    assert own_record is not None
    assert cross_record is None


def test_dify_mixed_wrapper_is_normalized(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    response = client.post(
        "/compat/dify/v1/chat-messages",
        headers=auth_headers,
        json={
            "inputs": {"patientId": {"type": "mixed", "value": "461"}},
            "query": "查一下患者信息",
            "response_mode": "blocking",
            "conversation_id": "mixed-wrapper",
            "user": "user-a",
        },
    )
    assert response.status_code == 200
