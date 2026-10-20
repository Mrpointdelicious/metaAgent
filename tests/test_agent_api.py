"""
创建日期：2026-10-10
文件功能：验证Run前端接口、断线回放、身份隔离、动作ACK和统一错误契约。
"""

import asyncio
import json

import httpx
import pytest

from meta_agent.app import create_app
from meta_agent.application.service import ApplicationRequest
from meta_agent.contracts import ActionPayload, RunRecord, fingerprint
from meta_agent.events.agent import AgentOutputAdapter, RunSubscription
from meta_agent.events.publisher import RunEventPublisher
from meta_agent.events.stream import EventEmitter
from meta_agent.orchestration.identity import TrustedScope
from tests.helpers.runtime import runtime, scene_response


def body(query="你好", request_id="frontend-1", **changes):
    return {"request_id": request_id, "query": query, **changes}


def headers(auth_headers):
    return {**auth_headers, "X-End-User-ID": "actor", "X-Tenant-ID": "tenant"}


def decoded(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def test_run_creation_snapshot_and_replay_contract(client, auth_headers):
    claims = headers(auth_headers)
    response = client.post("/v1/agent/runs", headers=claims, json=body())
    assert response.status_code == 202
    created = response.json()
    assert set(created) == {
        "request_id",
        "run_id",
        "conversation_id",
        "status",
        "events_url",
        "snapshot_url",
    }
    assert created["status"] == "accepted"
    response = client.get(created["events_url"], headers=claims)
    events = decoded(response.text)
    assert events[0]["type"] == "accepted" and events[-1]["type"] == "completed"
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert [int(x[4:]) for x in response.text.splitlines() if x.startswith("id: ")] == [
        e["seq"] for e in events
    ]
    assert "contract_type" not in events[0]
    snapshot = client.get(created["snapshot_url"], headers=claims).json()
    assert set(snapshot) == {
        "request_id",
        "run_id",
        "conversation_id",
        "status",
        "last_seq",
        "created_at",
    }
    assert snapshot["last_seq"] == events[-1]["seq"] and snapshot["status"] == "succeeded"
    repeat = client.post("/v1/agent/runs", headers=claims, json=body())
    assert repeat.status_code == 202 and repeat.json() == created
    assert (
        decoded(client.get(created["events_url"] + "?after_seq=1", headers=claims).text)
        == events[1:]
    )
    assert (
        decoded(client.get(created["events_url"], headers={**claims, "Last-Event-ID": "1"}).text)
        == events[1:]
    )
    assert (
        decoded(
            client.get(
                created["events_url"] + "?after_seq=0", headers={**claims, "Last-Event-ID": "bad"}
            ).text
        )
        == events
    )
    assert (
        client.get(
            created["events_url"] + f"?after_seq={snapshot['last_seq']}", headers=claims
        ).text
        == ""
    )


@pytest.mark.parametrize(
    "change",
    [
        {"query": "不同内容"},
        {"context": {"current_view": "different"}},
        {"context": {"patient_id": "462"}},
        {"conversation_id": "different"},
    ],
)
def test_request_id_conflicts_across_context_and_patient(client, auth_headers, change):
    claims = headers(auth_headers)
    client.post("/v1/agent/runs", headers=claims, json=body())
    response = client.post("/v1/agent/runs", headers=claims, json=body(**change))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"


@pytest.mark.parametrize(
    "change",
    [
        {"X-End-User-ID": "another"},
        {"X-Tenant-ID": "another"},
        {"X-User-Role": "clinician"},
    ],
)
def test_all_run_endpoints_are_owner_scoped(client, auth_headers, change):
    claims = headers(auth_headers)
    created = client.post("/v1/agent/runs", headers=claims, json=body()).json()
    path = created["snapshot_url"]
    other = {**claims, **change}
    for response in [
        client.get(path, headers=other),
        client.get(path + "/events", headers=other),
        client.post(path + "/cancel", headers=other),
        client.post(path + "/actions/no/ack", headers=other, json={"status": "executed"}),
    ]:
        assert response.status_code == 404 and response.json()["error"]["code"] == "run_not_found"


@pytest.mark.parametrize(
    "invalid",
    [
        {"query": " "},
        {"context": {"callback_url": "http://private"}},
        {"context": {"patient_id": "-1"}},
        {"context": {"scene_version": True}},
        {"context": {"selected_session_ref": "some-record"}},
        {"context": {"space_id": " "}},
        {"role": "operator"},
        {"tool_url": "http://private"},
    ],
)
def test_invalid_requests_have_safe_error_envelope(client, auth_headers, invalid):
    response = client.post(
        "/v1/agent/runs", headers=headers(auth_headers), json={**body(), **invalid}
    )
    assert response.status_code == 422
    assert set(response.json()) == {"error"}
    assert response.json()["error"]["code"] == "invalid_request"
    assert "http://private" not in response.text


def test_auth_cursor_unknown_action_and_cancel_errors(client, auth_headers):
    assert client.post("/v1/agent/runs", json=body()).json()["error"]["code"] == "unauthenticated"
    assert client.post("/v1/agent/runs", headers=auth_headers, json=body()).status_code == 401
    claims = headers(auth_headers)
    created = client.post("/v1/agent/runs", headers=claims, json=body()).json()
    for suffix in ("?after_seq=-1", "?after_seq=999999", "?after_seq=bad"):
        response = client.get(created["events_url"] + suffix, headers=claims)
        assert response.status_code == 422 and "error" in response.json()
    assert (
        client.get(
            created["events_url"], headers={**claims, "Last-Event-ID": "event_old"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            created["snapshot_url"] + "/actions/missing/ack",
            headers=claims,
            json={"status": "executed"},
        ).status_code
        == 404
    )
    assert client.post(created["snapshot_url"] + "/cancel", headers=claims).json() == {
        "accepted": True
    }


def test_storage_errors_do_not_expose_internal_details(client, auth_headers, monkeypatch):
    async def broken(*args, **kwargs):
        raise OSError("postgres://secret@localhost/db")

    monkeypatch.setattr(client.app.state.container.application, "start", broken)
    response = client.post("/v1/agent/runs", headers=headers(auth_headers), json=body())
    assert response.status_code == 503 and response.json()["error"]["retryable"]
    assert "secret" not in response.text and "postgres" not in response.text


def test_openapi_documents_typed_responses_and_sse(client):
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    create = paths["/v1/agent/runs"]["post"]
    assert create["responses"]["202"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "CreatedRunResponse"
    )
    assert create["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "ErrorResponse"
    )
    assert (
        "text/event-stream"
        in paths["/v1/agent/runs/{run_id}/events"]["get"]["responses"]["200"]["content"]
    )


def test_live_subscription_close_resume_and_independent_subscribers():
    async def check():
        async with runtime(scene_actions_enabled=True) as (c, backend):
            backend.overrides["navigate_scene"] = scene_response
            backend.gates["navigate_scene"] = asyncio.Event()
            scope = TrustedScope("tenant", "actor", space_id="space", scene_version=7)
            req = ApplicationRequest(
                "打开面板",
                scope,
                "c",
                "r",
                context={"current_view": "home"},
                owner_key=scope.principal_hash,
            )
            run = await c.application.start(req)
            first = RunSubscription(run.emitter, AgentOutputAdapter(), 0).stream()
            assert decoded(await anext(first))[0]["type"] == "accepted"
            await first.aclose()
            assert not run.emitter.detached and not run.task.done()
            second = RunSubscription(run.emitter, AgentOutputAdapter(), 1).stream()
            third = RunSubscription(run.emitter, AgentOutputAdapter(), 1).stream()

            async def drain(stream):
                return [decoded(frame)[0] async for frame in stream]

            readers = [asyncio.create_task(drain(s)) for s in (second, third)]
            backend.gates["navigate_scene"].set()
            left, right = await asyncio.wait_for(asyncio.gather(*readers), 2)
            assert left == right and left[-1]["type"] == "completed"
            assert [e["seq"] for e in left] == list(range(2, len(run.record.events) + 1))
            assert sum(e["type"] == "action_ready" for e in left) == 1
            action = next(e for e in left if e["type"] == "action_ready")
            assert "source_evidence_id" not in action["payload"]
            assert set(run.record.action_delivery.values()) == {"dispatched"}
            assert len(backend.calls) == 1

    asyncio.run(check())


def test_http_ack_is_durable_idempotent_and_separate_from_delivery():
    async def check():
        async with runtime(scene_actions_enabled=True, service_bearer_token="test-token") as (
            c,
            backend,
        ):
            backend.overrides["navigate_scene"] = scene_response
            app = create_app(c.settings)
            app.state.container = c
            claims = {"Authorization": "Bearer test-token", "X-End-User-ID": "actor"}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test", headers=claims
            ) as client:
                created = (
                    await client.post(
                        "/v1/agent/runs",
                        json=body("打开面板", context={"space_id": "space", "scene_version": 7}),
                    )
                ).json()
                events = decoded((await client.get(created["events_url"])).text)
                action = next(e for e in events if e["type"] == "action_ready")
                path = created["snapshot_url"] + f"/actions/{action['payload']['action_id']}/ack"
                ack = await client.post(path, json={"status": "executed"})
                assert ack.status_code == 200 and ack.json()["accepted"]
                assert (await client.post(path, json={"status": "executed"})).json() == ack.json()
                assert (
                    await client.post(path, json={"status": "failed", "reason": "scene_not_ready"})
                ).status_code == 409
                event_replay = decoded((await client.get(created["events_url"])).text)
                assert event_replay == events
                identity = TrustedScope(c.settings.default_tenant_id, "actor")
                run = await c.application.owned_run(identity.principal_hash, created["run_id"])
                key = fingerprint([created["run_id"], action["payload"]["action_id"]])
                stored = await c.repository.get("action_acks", run.record.scope_key, key)
                assert stored["status"] == "executed"
                assert set(run.record.action_delivery.values()) == {"dispatched"}
                assert len(backend.calls) == 1

    asyncio.run(check())


def test_orphan_run_recovery_is_single_terminal_and_does_not_execute_again():
    async def check():
        async with runtime() as (c, backend):
            scope = TrustedScope("tenant", "actor")
            record = RunRecord(
                run_id="interrupted",
                request_id="old",
                request_hash="hash",
                scope_key=scope.scope_hash,
                conversation_id="c",
            )
            emitter = EventEmitter(record, c.repository)
            publisher = RunEventPublisher(emitter)
            await publisher.accepted()
            await publisher.action(
                ActionPayload(
                    action_id="action_old",
                    command_code="[ClickPoint:]1",
                    source_evidence_id="e",
                    source_space_id="space",
                    scene_version=7,
                    command_order=1,
                ),
                task_id="t",
                goal_id="g",
            )
            await c.repository.put(
                "agent_runs",
                scope.principal_hash,
                record.run_id,
                {"scope_key": scope.scope_hash},
                60,
            )
            runs = await asyncio.gather(
                *(c.application.owned_run(scope.principal_hash, record.run_id) for _ in range(3))
            )
            for run in runs:
                frames = [
                    x async for x in RunSubscription(run.emitter, AgentOutputAdapter(), 1).stream()
                ]
                assert [e["type"] for x in frames for e in decoded(x)] == [
                    "action_ready",
                    "completed",
                ]
                assert run.record.status == "cancelled"
                assert sum(e.type == "completed" for e in run.record.events) == 1
            assert not backend.calls
            await c.repository.delete("agent_runs", scope.principal_hash, record.run_id)
            assert await c.application.owned_run(scope.principal_hash, record.run_id) is None

    asyncio.run(check())


def test_admission_limit_preserves_retry_and_explicit_cancel_releases_capacity():
    async def check():
        async with runtime(
            max_concurrent_requests=1, scene_actions_enabled=True, service_bearer_token="token"
        ) as (c, backend):
            backend.overrides["navigate_scene"] = scene_response
            backend.gates["navigate_scene"] = asyncio.Event()
            app = create_app(c.settings)
            app.state.container = c
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer token", "X-End-User-ID": "actor"},
            ) as client:
                payload = body("打开面板", context={"space_id": "space", "scene_version": 7})
                initial = await client.post("/v1/agent/runs", json=payload)
                assert initial.status_code == 202
                repeat = await client.post("/v1/agent/runs", json=payload)
                assert repeat.status_code == 202 and repeat.json() == initial.json()
                busy = await client.post("/v1/agent/runs", json=body(request_id="new"))
                assert busy.status_code == 429 and busy.json()["error"]["retryable"]
                assert (await client.post(initial.json()["snapshot_url"] + "/cancel")).json() == {
                    "accepted": True
                }
                events = decoded((await client.get(initial.json()["events_url"])).text)
                assert events[-1]["payload"]["outcome"] == "cancelled"
                assert not c.application.active
                assert (
                    await client.post("/v1/agent/runs", json=body(request_id="new"))
                ).status_code == 202

    asyncio.run(check())


def test_cancellation_during_terminal_save_waits_for_committed_completion(monkeypatch):
    async def check():
        async with runtime(max_concurrent_requests=1) as (c, _):
            scope = TrustedScope("tenant", "actor")
            original = c.repository.save_run
            saving, release = asyncio.Event(), asyncio.Event()

            async def gated(record):
                if record.events and record.events[-1].type == "completed":
                    saving.set()
                    await release.wait()
                await original(record)

            monkeypatch.setattr(c.repository, "save_run", gated)
            run = await c.application.start(
                ApplicationRequest(
                    "你好",
                    scope,
                    "c",
                    "r",
                    context={"current_view": "home"},
                    owner_key=scope.principal_hash,
                )
            )
            await asyncio.wait_for(saving.wait(), 1)
            cancellations = [
                asyncio.create_task(c.application.cancel(scope.scope_hash, run.record.run_id))
                for _ in range(3)
            ]
            await asyncio.sleep(0)
            assert not any(t.done() for t in cancellations)
            release.set()
            await asyncio.wait_for(asyncio.gather(*cancellations), 1)
            assert run.record.status == "succeeded" and not run.emitter.persistence_failed
            assert sum(e.type == "completed" for e in run.record.events) == 1
            assert not c.application.active and not c.application.admission_limiter.locked()

    asyncio.run(check())


def test_audit_failure_cannot_undo_committed_terminal_event(monkeypatch):
    async def check():
        async with runtime(max_concurrent_requests=1) as (c, _):
            original = c.repository.put

            async def broken(kind, *args, **kwargs):
                if kind == "audit":
                    raise OSError("audit unavailable")
                return await original(kind, *args, **kwargs)

            monkeypatch.setattr(c.repository, "put", broken)
            scope = TrustedScope("tenant", "actor")
            run = await c.application.execute(
                ApplicationRequest(
                    "你好",
                    scope,
                    "c",
                    "r",
                    context={"current_view": "home"},
                    owner_key=scope.principal_hash,
                )
            )
            assert run.record.events[-1].type == "completed" and run.record.status == "succeeded"
            assert not c.application.active and not c.application.admission_limiter.locked()

    asyncio.run(check())
