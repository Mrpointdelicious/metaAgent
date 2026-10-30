"""
创建日期：2026-10-30
文件功能：验证工作流事件、可信历史主体、多接入隔离和敏感错误清理。
"""

import asyncio
import json
from dataclasses import replace

import pytest
from fastapi import HTTPException

from meta_agent.adapters.dify import DifyWorkflowEvents
from meta_agent.agent.tools import invoke_tool, tool_allowed
from meta_agent.api.routes import dify_stream
from meta_agent.contracts import OutboundEvent, TaskResult
from meta_agent.orchestration.identity import TrustedScope
from tests.helpers.integration import context
from tests.helpers.runtime import runtime


class TestIdentityProvider:
    __test__ = False

    async def authenticate(self, request, client):
        sessions = {"alice-session": "alice", "bob-session": "bob"}
        user = sessions.get(request.headers.get("X-Test-Session"))
        if user is None:
            raise HTTPException(403, "Synthetic session required")
        return TrustedScope("tenant", user, space_id="space", scene_version=7)


def test_history_requires_business_session_and_never_trusts_query_user(client, auth_headers):
    assert client.get("/v1/conversations").status_code == 401
    assert client.get("/v1/conversations", headers=auth_headers).status_code == 503
    client.app.state.business_identity_provider = TestIdentityProvider()
    assert client.get("/v1/conversations", headers=auth_headers).status_code == 403
    alice = {**auth_headers, "X-Test-Session": "alice-session"}
    bob = {**auth_headers, "X-Test-Session": "bob-session"}
    response = client.post(
        "/v1/chat-messages",
        headers=alice,
        json={
            "user": "alice",
            "query": "你好",
            "response_mode": "blocking",
            "conversation_id": "same",
            "inputs": {"role": "operator"},
        },
    )
    assert response.status_code == 200
    conversation = response.json()["conversation_id"]
    own = client.get("/v1/messages", headers=alice, params={"conversation_id": conversation}).json()
    assert own["data"][0]["query"] == "你好"
    assert client.get("/v1/conversations", headers=alice).json()["data"][0]["id"] == conversation
    assert (
        client.get("/v1/messages", headers=bob, params={"conversation_id": conversation}).json()[
            "data"
        ]
        == []
    )
    assert client.get("/v1/conversations", headers=alice, params={"user": "bob"}).status_code == 403
    forged = client.post(
        "/v1/chat-messages",
        headers=alice,
        json={
            "user": "bob",
            "query": "你好",
            "response_mode": "blocking",
        },
    )
    assert forged.status_code == 403
    legacy = client.post(
        "/v1/chat-messages",
        headers=alice,
        json={
            "user": "alice",
            "query": "你好",
            "inputs": {"patientId": "forged"},
        },
    )
    assert legacy.status_code == 422


@pytest.mark.parametrize(
    "excluded,endpoint,capability",
    [
        ("search_online_doctors", "search_doctors", "doctors.search"),
        ("search_knowledge", "search_knowledge", "knowledge.search"),
        ("resolve_scene_instruction", "navigate_scene", "scene.resolve"),
    ],
)
def test_alias_restrictions_also_apply_to_canonical_and_legacy_execution(
    excluded, endpoint, capability
):
    from meta_agent.agent.tools import spec
    from meta_agent.contracts import DomainError
    from meta_agent.execution.dispatcher import DomainDispatcher

    async def check():
        async with runtime() as (container, backend):
            ctx = context(container)
            ctx.scope = replace(ctx.scope, excluded_tools=frozenset({excluded}))
            assert not tool_allowed(ctx, endpoint)
            with pytest.raises(DomainError) as direct:
                await ctx.call(endpoint, {})
            assert direct.value.code == "tool_not_allowed"
            with pytest.raises(DomainError) as legacy:
                await DomainDispatcher().execute(
                    spec(ctx, endpoint, "legacy", capability, {}), {}, ctx
                )
            assert legacy.value.code == "tool_not_allowed" and not backend.calls

    asyncio.run(check())


def test_secondary_client_isolates_history_and_cannot_bypass_tool_alias(client, auth_headers):
    settings = client.app.state.container.settings
    settings.service_bearer_token_secondary = "synthetic-secondary"
    settings.secondary_excluded_tools = ["search_doctors", "search_knowledge"]
    client.app.state.business_identity_provider = TestIdentityProvider()
    primary = {**auth_headers, "X-Test-Session": "alice-session"}
    secondary = {"Authorization": "Bearer synthetic-secondary", "X-Test-Session": "alice-session"}
    assert (
        client.post(
            "/v1/chat-messages",
            headers=primary,
            json={
                "user": "alice",
                "query": "你好",
                "response_mode": "blocking",
            },
        ).status_code
        == 200
    )
    assert client.get("/v1/conversations", headers=secondary).json()["data"] == []

    async def check():
        async with runtime() as (container, backend):
            ctx = context(container)
            ctx.scope = replace(
                ctx.scope,
                client_name="secondary",
                excluded_tools=frozenset(settings.secondary_excluded_tools),
            )
            for name in (
                "search_doctors",
                "search_online_doctors",
                "resolve_doctor_selection",
                "search_knowledge",
            ):
                assert not tool_allowed(ctx, name)
                result = await invoke_tool(ctx, name, {})
                assert result["code"] == "tool_not_allowed"
            assert not backend.calls

    asyncio.run(check())


def test_workflow_stream_and_blocking_have_identical_final_answer(client, auth_headers):
    client.app.state.container.settings.dify_workflow_events_enabled = True
    payload = {"user": "actor", "query": "你好", "inputs": {"apitoken": "synthetic-secret"}}
    blocking = client.post(
        "/v1/chat-messages", headers=auth_headers, json={**payload, "response_mode": "blocking"}
    ).json()
    streaming = client.post(
        "/v1/chat-messages", headers=auth_headers, json={**payload, "response_mode": "streaming"}
    )
    frames = [
        json.loads(line[6:]) for line in streaming.text.splitlines() if line.startswith("data: ")
    ]
    assert [frame["event"] for frame in frames] == [
        "workflow_started",
        "message",
        "workflow_finished",
        "message_end",
    ]
    assert frames[1]["answer"] == blocking["answer"] == frames[2]["data"]["outputs"]["answer"]
    assert "synthetic-secret" not in streaming.text


def test_workflow_nodes_do_not_guess_parallel_completion_or_export_raw_outputs():
    async def check():
        async with runtime(dify_workflow_events_enabled=True) as (container, _):
            ctx = context(container)
            record = ctx.record
            adapter = DifyWorkflowEvents(record)
            for i in range(2):
                event = OutboundEvent(
                    request_id=record.request_id,
                    run_id=record.run_id,
                    conversation_id=record.conversation_id,
                    seq=i + 1,
                    type="progress",
                    task_id=f"t{i}",
                    payload={"stage": "tool", "text": "query"},
                )
                assert adapter.progress(event)[0]["event"] == "node_started"
                record.results[f"t{i}"] = TaskResult(
                    task_id=f"t{i}",
                    status="failed" if i else "succeeded",
                    outputs={"password": "synthetic-secret"},
                )
            record.status = "partial"
            final = adapter.finished("final")
            assert [frame["data"]["status"] for frame in final] == [
                "succeeded",
                "failed",
                "succeeded",
            ]
            assert "synthetic-secret" not in json.dumps(final)

    asyncio.run(check())


def test_workflow_disconnect_before_first_progress_cancels_run():
    async def check():
        from types import SimpleNamespace

        async with runtime(dify_workflow_events_enabled=True) as (container, _):
            from tests.helpers.runtime import request

            run = await container.application.start(request("你好"))
            req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(container=container)))
            generator = dify_stream(run, req)
            assert "workflow_started" in await anext(generator)
            await generator.aclose()
            assert run.emitter.closed
            assert run.record.status == "cancelled"

    asyncio.run(check())


def test_repeated_revisions_of_the_same_original_answer_do_not_append_old_text():
    from meta_agent.events.dify import DifyEventAdapter

    async def check():
        async with runtime() as (container, _):
            ctx = context(container)
            for index, (event_id, replaces, text) in enumerate(
                [
                    ("original", None, "旧答案"),
                    ("revision1", "original", "中间答案"),
                    ("revision2", "original", "最终答案"),
                ]
            ):
                ctx.record.events.append(
                    OutboundEvent(
                        event_id=event_id,
                        request_id=ctx.record.request_id,
                        run_id=ctx.record.run_id,
                        conversation_id=ctx.record.conversation_id,
                        seq=index + 1,
                        type="answer_part",
                        payload={"text": text, "revision": index + 1, "replaces": replaces},
                    )
                )
            assert DifyEventAdapter().message(ctx.record)["answer"] == "最终答案"

    asyncio.run(check())


def test_history_pagination_and_expiry_do_not_cross_scope():
    async def check():
        async with runtime() as (container, _):
            repository = container.repository
            for index in range(3):
                ctx = context(container)
                ctx.record.conversation_id = "shared"
                ctx.record.status = "succeeded"
                await repository.save_history(ctx.record, f"question{index}")
            scope = ctx.scope.scope_hash
            first, more = await repository.history_messages(scope, "shared", limit=2)
            assert len(first) == 2 and more
            last, more = await repository.history_messages(
                scope, "shared", limit=2, first_id=first[-1]["id"]
            )
            assert len(last) == 1 and not more
            assert not {row["id"] for row in first} & {row["id"] for row in last}
            assert await repository.history_messages("other-scope", "shared") == ([], False)
            await repository.put("history_messages", scope, last[0]["id"], last[0], -1)
            rows, _ = await repository.history_messages(scope, "shared")
            assert len(rows) == 2

    asyncio.run(check())


@pytest.mark.parametrize("endpoint", ["/v1/chat", "/v1/chat-messages"])
def test_validation_errors_do_not_echo_credentials(client, auth_headers, endpoint):
    body = {
        "user": "actor",
        "query": "hello",
        "request_id": "r",
        "inputs": {"identity_context_token": "synthetic-secret"},
        "response_mode": {"secret": "synthetic-secret"},
    }
    response = client.post(endpoint, headers=auth_headers, json=body)
    assert response.status_code == 422 and "synthetic-secret" not in response.text
