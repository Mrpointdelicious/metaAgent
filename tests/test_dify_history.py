"""
创建日期：2026-10-15
文件功能：验证 Dify 兼容的会话列表与消息历史接口。
"""

import pytest


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
