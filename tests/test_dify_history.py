"""
创建日期：2026-10-15
文件功能：验证 Dify 兼容的会话列表与消息历史接口。
"""

from unittest.mock import ANY, AsyncMock

import pytest

from meta_agent.application.report_history import report_turn


def chat_payload(query, conversation_id=""):
    return {
        "query": query,
        "user": "actor",
        "conversation_id": conversation_id,
        "inputs": {},
        "response_mode": "blocking",
    }


def test_conversation_history_listed_and_messages(client, auth_headers):
    first = client.post("/v1/chat-messages", headers=auth_headers, json=chat_payload("你好")).json()
    cid = first["conversation_id"]
    second = client.post(
        "/v1/chat-messages", headers=auth_headers, json=chat_payload("你好", cid)
    ).json()
    assert first["event"] == "message" and second["event"] == "message"

    conversations = client.get(
        "/v1/conversations", headers=auth_headers, params={"user": "actor", "limit": 20}
    ).json()
    assert conversations["has_more"] is False
    assert len(conversations["data"]) == 1
    conv = conversations["data"][0]
    assert conv["id"] == cid
    assert conv["name"] == "你好"
    assert conv["status"] == "normal"

    messages = client.get(
        "/v1/messages",
        headers=auth_headers,
        params={"user": "actor", "conversation_id": cid},
    ).json()
    assert messages["has_more"] is False
    assert len(messages["data"]) == 2
    assert all(m["conversation_id"] == cid for m in messages["data"])
    assert all(m["query"] == "你好" for m in messages["data"])
    assert all(m["answer"] for m in messages["data"])


def test_messages_returns_full_history_beyond_six_turns(client, auth_headers):
    # 回归：会话历史曾硬截断到最近 6 轮，导致 /v1/messages 只返回最近几条。
    cid = ""
    for i in range(10):
        response = client.post(
            "/v1/chat-messages", headers=auth_headers, json=chat_payload(f"问题{i}", cid)
        )
        assert response.json()["event"] == "message"
        cid = response.json()["conversation_id"]
    messages = client.get(
        "/v1/messages",
        headers=auth_headers,
        params={"user": "actor", "conversation_id": cid, "limit": 100},
    ).json()
    assert len(messages["data"]) == 10
    assert [m["query"] for m in messages["data"]] == [f"问题{i}" for i in reversed(range(10))]


def test_messages_missing_conversation_returns_404(client, auth_headers):
    response = client.get(
        "/v1/messages",
        headers=auth_headers,
        params={"user": "actor", "conversation_id": "nope"},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("endpoint", ["/v1/conversations", "/v1/messages"])
def test_history_endpoints_require_auth(client, endpoint):
    assert client.get(endpoint, params={"user": "actor"}).status_code == 401


def test_doctor_history_uses_recorded_role_when_get_has_only_user(client, auth_headers):
    payload = chat_payload("医生普通问答")
    payload["inputs"] = {"identity": "医生", "pos": "二楼 问诊中心"}
    created = client.post("/v1/chat-messages", headers=auth_headers, json=payload)
    cid = created.json()["conversation_id"]
    for role_params in ({}, {"identity": "医生"}, {"role": "clinician"}):
        listed = client.get(
            "/v1/conversations", headers=auth_headers, params={"user": "actor", **role_params}
        )
        assert listed.status_code == 200
        assert [r["id"] for r in listed.json()["data"]] == [cid]
        messages = client.get(
            "/v1/messages",
            headers=auth_headers,
            params={
                "user": "actor",
                "conversation_id": cid,
                **role_params,
            },
        )
        assert messages.status_code == 200
        assert messages.json()["data"][0]["query"] == "医生普通问答"
    for params in (
        {"user": "other"},
        {"user": "actor", "tenant_id": "other"},
        {"user": "actor", "role": "patient"},
    ):
        listed = client.get("/v1/conversations", headers=auth_headers, params=params)
        assert listed.json()["data"] == []
        messages = client.get(
            "/v1/messages", headers=auth_headers, params={**params, "conversation_id": cid}
        )
        assert messages.status_code == 404


def test_multi_role_history_requires_selection_instead_of_combining_roles(client, auth_headers):
    conversations = {}
    for role in ("patient", "clinician"):
        payload = chat_payload(role)
        payload["inputs"] = {"role": role}
        result = client.post("/v1/chat-messages", headers=auth_headers, json=payload)
        conversations[role] = result.json()["conversation_id"]
    listed = client.get("/v1/conversations", headers=auth_headers, params={"user": "actor"})
    assert listed.status_code == 422
    assert listed.json()["detail"]["code"] == "history_identity_required"
    for role, cid in conversations.items():
        listed = client.get(
            "/v1/conversations", headers=auth_headers, params={"user": "actor", "role": role}
        )
        assert [r["id"] for r in listed.json()["data"]] == [cid]
        # A specific conversation ID itself identifies one recorded role.
        messages = client.get(
            "/v1/messages", headers=auth_headers, params={"user": "actor", "conversation_id": cid}
        )
        assert messages.status_code == 200 and messages.json()["data"][0]["query"] == role


def test_implicit_history_role_does_not_cross_gateway_or_patient_scope(
    client, settings, auth_headers
):
    from tests.helpers.identity import patient_inputs

    settings.service_bearer_token_secondary = "secondary-test"
    payload = chat_payload("医生普通问答")
    payload["inputs"] = {"identity": "医生"}
    cid = client.post("/v1/chat-messages", headers=auth_headers, json=payload).json()[
        "conversation_id"
    ]
    secondary = {"Authorization": "Bearer secondary-test"}
    assert (
        client.get("/v1/conversations", headers=secondary, params={"user": "actor"}).json()["data"]
        == []
    )
    assert (
        client.get(
            "/v1/messages", headers=secondary, params={"user": "actor", "conversation_id": cid}
        ).status_code
        == 404
    )
    payload.update(user="patient-user", inputs=patient_inputs("471"))
    cid = client.post("/v1/chat-messages", headers=auth_headers, json=payload).json()[
        "conversation_id"
    ]
    assert (
        client.get(
            "/v1/conversations", headers=auth_headers, params={"user": "patient-user"}
        ).json()["data"]
        == []
    )
    assert (
        client.get(
            "/v1/messages",
            headers=auth_headers,
            params={"user": "patient-user", "conversation_id": cid},
        ).status_code
        == 404
    )
    authorized = client.post(
        "/v1/messages/query",
        headers=auth_headers,
        params={"conversation_id": cid},
        json={"user": "patient-user", "inputs": payload["inputs"]},
    )
    assert authorized.status_code == 200 and authorized.json()["data"]


def test_relaxed_history_lists_patient_scope_conversation_with_only_user(
    client, settings, auth_headers, monkeypatch
):
    """测试服口径：Dify 历史页只传 user 时，仍能看到带患者/空间参数写入的会话。"""
    from tests.helpers.identity import patient_inputs

    ref = "irego_scope_v2_" + "a" * 64
    grant = "patient_artifact_v2.payload." + "b" * 43
    report_url = f"https://reports.test/api/ai/patients/report-artifacts/{ref}/content"
    monkeypatch.setattr(
        "meta_agent.application.service.report_turn",
        lambda user, answer, created_at: report_turn(
            user, f"{answer}\n{report_url}?access_grant={grant}", created_at
        ),
    )
    payload = chat_payload("带患者身份的问题")
    payload["user"] = "relaxed-user"
    payload["inputs"] = patient_inputs("0003kh", identity_type="login_name", spaceid="space-a")
    cid = client.post("/v1/chat-messages", headers=auth_headers, json=payload).json()[
        "conversation_id"
    ]
    # 严格模式（正式服默认）：仍不可见。
    assert (
        client.get(
            "/v1/conversations", headers=auth_headers, params={"user": "relaxed-user"}
        ).json()["data"]
        == []
    )

    settings.history_scope_relaxed = True
    listed = client.get(
        "/v1/conversations", headers=auth_headers, params={"user": "relaxed-user"}
    ).json()
    assert [r["id"] for r in listed["data"]] == [cid]
    messages = client.get(
        "/v1/messages",
        headers=auth_headers,
        params={"user": "relaxed-user", "conversation_id": cid},
    )
    assert messages.status_code == 200
    assert messages.json()["data"][0]["query"] == "带患者身份的问题"
    assert messages.json()["data"][0]["report_artifacts"] == [ref]
    assert grant not in messages.text
    # 放宽只针对患者身份/空间差异，不跨用户。
    assert (
        client.get(
            "/v1/conversations", headers=auth_headers, params={"user": "someone-else"}
        ).json()["data"]
        == []
    )

    # Image reads require the original scope and forward the grant stored with the message.
    fetch_image = AsyncMock(return_value=b"png")
    monkeypatch.setattr(
        client.app.state.container.ai_webapi_client, "read_report_image", fetch_image
    )
    params = {
        "conversation_id": cid,
        "message_id": messages.json()["data"][0]["id"],
        "artifact_ref": ref,
    }
    denied = client.get(
        "/v1/messages", headers=auth_headers, params={**params, "user": payload["user"]}
    )
    assert denied.status_code == 404
    fetch_image.assert_not_awaited()
    allowed = client.post(
        "/v1/messages/query",
        headers=auth_headers,
        params=params,
        json={"user": payload["user"], "inputs": payload["inputs"]},
    )
    assert allowed.status_code == 200 and allowed.content == b"png"
    fetch_image.assert_awaited_once_with(ref, grant, ANY, cid)
