"""
创建日期：2026-11-07
文件功能：验证真实旧键格式的普通会话迁移、续聊和历史读取的归属及过期边界。
"""

import asyncio
from dataclasses import replace

import httpx
import pytest

from meta_agent.app import create_app
from meta_agent.application.conversations import owner_key
from meta_agent.contracts import ConversationState, Goal
from meta_agent.orchestration.identity import TrustedScope
from tests.helpers.runtime import runtime
from tests.test_consultation_sessions import activate

USER = "业务用户-001"
CID = "conversation_legacy001"
# Generated with the 70b547ba thread format, not the migration helper under test:
# scope=["default",USER,"clinician",null], thread=[sha256(scope),CID].
OLD_THREAD = "ae49ba828e29c97d36dbb13129bb61bfc37717ece4d0626672fc811a787ec197"
SCOPE = TrustedScope("default", USER, role="clinician")
INPUTS = {"identity": "医生", "pos": "二楼 问诊中心", "spaceid": "space"}


async def seed_legacy(repository, *, inputs=None, state_changes=None):
    state = ConversationState(
        turns=[{"user": "旧问题", "assistant": "旧回答", "created_at": 1700000000}],
        agent_messages=[{"type": "tool", "data": {"content": "old-tool-payload"}}],
        pending_goals=[
            Goal(goal_id="pending", kind="scene_action", domain="scene", query_span="去二楼")
        ],
    )
    if state_changes:
        state = state.model_copy(update=state_changes)
    await repository.save_conversation(OLD_THREAD, state)
    await repository.save_conversation_index(
        USER,
        CID,
        OLD_THREAD,
        {**INPUTS, "apikey": "old-secret-key", **(inputs or {})},
        "旧问题",
    )
    return await repository.conversation_meta(USER, CID)


def app_for(container):
    app = create_app(container.settings)
    app.state.container = container
    return app


@pytest.mark.parametrize("entry", ["chat", "messages", "list", "post_messages"])
def test_verified_old_id_restores_history_and_sanitizes_runtime_state(entry):
    async def check():
        async with runtime(legacy_conversation_client="primary", service_bearer_token="test") as (
            container,
            _,
        ):
            repository = container.repository
            old_meta = await seed_legacy(repository)
            model = activate(container, ["继续回答"])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_for(container)),
                base_url="http://test",
                headers={"Authorization": "Bearer test"},
            ) as client:
                if entry == "chat":
                    response = await client.post(
                        "/v1/chat-messages",
                        json={
                            "query": "继续",
                            "user": USER,
                            "inputs": INPUTS,
                            "conversation_id": CID,
                            "response_mode": "blocking",
                        },
                    )
                    assert response.status_code == 200
                    assert response.json()["conversation_id"] == CID
                    assert "旧问题" in model.seen[0] and "旧回答" in model.seen[0]
                    assert "old-tool-payload" not in model.seen[0]
                elif entry == "list":
                    response = await client.get("/v1/conversations", params={"user": USER})
                    assert response.status_code == 200
                    assert [r["id"] for r in response.json()["data"]] == [CID]
                elif entry == "post_messages":
                    response = await client.post(
                        "/v1/messages/query",
                        params={"conversation_id": CID},
                        json={"user": USER, "inputs": INPUTS},
                    )
                    assert response.status_code == 200
                    assert response.json()["data"][0]["answer"] == "旧回答"
                messages = await client.get(
                    "/v1/messages",
                    params={
                        "user": USER,
                        "conversation_id": CID,
                    },
                )
                assert messages.status_code == 200
                assert messages.json()["data"][-1]["query"] == "旧问题"
                assert messages.json()["data"][-1]["answer"] == "旧回答"
                assert messages.json()["data"][-1]["created_at"] == 1700000000
            selector = container.application.conversations
            binding = await selector.get(SCOPE, CID)
            assert binding.authorization_scope == SCOPE.scope_hash
            assert binding.agent_kind == "default"
            state = await repository.conversation(SCOPE.thread_id(CID))
            assert not state.pending_goals and not state.variables and state.current_record is None
            assert "old-tool-payload" not in str(state.agent_messages)
            meta = await repository.conversation_meta(owner_key(SCOPE), CID)
            assert meta["thread_id"] == SCOPE.thread_id(CID)
            assert meta["inputs"]["space_id"] == "space" and "apikey" not in meta["inputs"]
            assert await repository.conversation_meta(USER, CID) == old_meta
            assert len(state.turns) == (2 if entry == "chat" else 1)
            # A second lookup is idempotent and does not restore the old snapshot.
            await selector.lookup(SCOPE, CID)
            assert (await repository.conversation(SCOPE.thread_id(CID))).turns == state.turns

    asyncio.run(check())


@pytest.mark.parametrize(
    "case",
    [
        "disabled",
        "wrong_client",
        "wrong_user",
        "wrong_role",
        "wrong_tenant",
        "unknown_id",
        "expired_index",
        "expired_state",
        "missing_state",
        "wrong_thread",
        "patient_binding",
        "consultation_room",
        "private_variables",
        "old_category",
        "invalid_metadata",
    ],
)
def test_unverifiable_or_expired_old_conversations_are_not_recovered(case):
    async def check():
        async with runtime(legacy_conversation_client="" if case == "disabled" else "primary") as (
            container,
            _,
        ):
            repo = container.repository
            inputs = {"patientId": "471"} if case == "patient_binding" else {}
            if case == "consultation_room":
                inputs["pos"] = "三楼 问诊房间"
            changes = {"variables": {"record": "private"}} if case == "private_variables" else {}
            if case == "old_category":
                changes["previous_category"] = "问诊"
            meta = await seed_legacy(repo, inputs=inputs, state_changes=changes)
            if case in {"expired_index", "wrong_thread", "invalid_metadata"}:
                if case == "wrong_thread":
                    meta["thread_id"] = "not-the-owned-thread"
                if case == "invalid_metadata":
                    meta.pop("created_at")
                await repo.put(
                    "conversation_index", USER, CID, meta, -1 if case == "expired_index" else 3600
                )
            if case == "expired_state":
                raw = await repo.get("conversations", OLD_THREAD, "state")
                await repo.put("conversations", OLD_THREAD, "state", raw, -1)
            if case == "missing_state":
                await repo.delete("conversations", OLD_THREAD, "state")
            scope = SCOPE
            if case == "wrong_client":
                scope = replace(scope, client_id="secondary")
            elif case == "wrong_user":
                scope = replace(scope, end_user_id="different-user")
            elif case == "wrong_role":
                scope = replace(scope, role="patient")
            elif case == "wrong_tenant":
                scope = replace(scope, tenant_id="another-tenant")
            cid = "unknown" if case == "unknown_id" else CID
            assert await container.application.conversations.lookup(scope, cid) is None
            assert await repo.conversation_meta(owner_key(scope), cid) is None
            assert await repo.get("conversations", scope.thread_id(cid), "state") is None

    asyncio.run(check())


def test_migration_resumes_without_overwriting_newer_conversation_state():
    async def check():
        async with runtime(legacy_conversation_client="primary") as (c, _):
            await seed_legacy(c.repository)
            newer = ConversationState(turns=[{"user": "新问题", "assistant": "新回答"}])
            await c.repository.save_conversation(SCOPE.thread_id(CID), newer)
            assert await c.application.conversations.lookup(SCOPE, CID) is not None
            assert (await c.repository.conversation(SCOPE.thread_id(CID))).turns == newer.turns

    asyncio.run(check())
