"""
创建日期：2026-10-09
文件功能：验证并发事件发布、游标交付、持久化失败及断线和快照语义。
"""

import asyncio

import httpx
import pytest

from meta_agent.app import create_app
from meta_agent.contracts import ActionPayload, ArtifactPayload, DomainError, RunRecord
from meta_agent.events.delivery import EventDelivery
from meta_agent.events.native import NativeOutputAdapter, SnapshotPurpose
from meta_agent.events.publisher import AnswerContent, RunEventPublisher, RunOutcome
from meta_agent.events.stream import EventEmitter, EventPersistenceError
from tests.helpers.runtime import request, runtime, scene_response


class RecordingRepository:
    def __init__(self):
        self.saved = []
        self.fail = False
        self.gate = None
        self.entered = asyncio.Event()

    async def save_run(self, record):
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        if self.fail:
            raise OSError("fixture persistence failure")
        self.saved.append(record.model_copy(deep=True))


def setup():
    record = RunRecord(
        run_id="r", request_id="q", request_hash="h", scope_key="s", conversation_id="c"
    )
    repository = RecordingRepository()
    emitter = EventEmitter(record, repository)
    return RunEventPublisher(emitter), emitter, repository


def outcome():
    return RunOutcome(outcome="succeeded", goal_statuses={}, task_statuses={})


def action(action_id="a"):
    return ActionPayload(
        action_id=action_id,
        command_code="[ClickPoint:]1",
        source_evidence_id="e",
        source_space_id="s",
        scene_version=1,
        command_order=1,
    )


def test_no_consumer_can_publish_over_queue_capacity_and_finish():
    async def check():
        publisher, emitter, repository = setup()
        async with asyncio.timeout(2):
            await publisher.accepted()
            await asyncio.gather(*[publisher.progress("tool", task_id=str(i)) for i in range(200)])
            await publisher.completed(outcome())
            await emitter.finish()
            await emitter.finish()
        cursor, delivered = 0, []
        while (event := await emitter.next_event(cursor)) is not None:
            delivered.append(event)
            cursor = event.seq
        assert [e.seq for e in delivered] == list(range(1, 203))
        assert delivered[-1].payload["event_range"] == {"first_seq": 1, "last_seq": 202}
        assert repository.saved[-1].events == delivered
        with pytest.raises(RuntimeError, match="finished"):
            await publisher.progress("tool")

    asyncio.run(check())


def test_concurrent_revision_artifact_action_and_completion_rules():
    async def check():
        publisher, emitter, _ = setup()
        await asyncio.gather(
            *[publisher.answer(AnswerContent(text="one"), goal_id="g") for _ in range(20)]
        )
        await asyncio.gather(
            *[publisher.answer(AnswerContent(text=str(i)), goal_id="g") for i in range(3)]
        )
        artifact = ArtifactPayload(
            artifact_ref="ref", url="https://example.test/r.png", evidence_id="e"
        )
        await asyncio.gather(*[publisher.artifact(artifact) for _ in range(20)])
        await publisher.action(action("a"), task_id="t1", goal_id="g1")
        await publisher.action(action("b"), task_id="t2", goal_id="g2")
        await publisher.action(action("a"), task_id="t1", goal_id="g1")
        terminal = await asyncio.gather(*[publisher.completed(outcome()) for _ in range(10)])
        assert len({e.event_id for e in terminal}) == 1
        answers = [e for e in emitter.record.events if e.type == "answer_part"]
        assert [e.payload["revision"] for e in answers] == [1, 2, 3, 4]
        assert [e.payload["replaces"] for e in answers] == [None] + [
            e.event_id for e in answers[:-1]
        ]
        assert sum(e.type == "artifact_ready" for e in emitter.record.events) == 1
        assert sum(e.type == "action_ready" for e in emitter.record.events) == 2
        assert sum(e.type == "completed" for e in emitter.record.events) == 1

    asyncio.run(check())


def test_consumer_waits_for_successful_persistence_and_wakes_on_finish():
    async def check():
        publisher, emitter, repository = setup()
        repository.gate = asyncio.Event()
        consumer = asyncio.create_task(emitter.next_event(0))
        producer = asyncio.create_task(publisher.accepted())
        await repository.entered.wait()
        assert not consumer.done()
        assert emitter.committed_seq == 0
        repository.gate.set()
        saved, received = await asyncio.wait_for(asyncio.gather(producer, consumer), 1)
        assert saved == received == repository.saved[-1].events[0]
        waiting = asyncio.create_task(emitter.next_event(received.seq))
        await emitter.finish()
        assert await asyncio.wait_for(waiting, 1) is None

    asyncio.run(check())


def test_failed_write_rolls_back_and_wakes_consumers_without_retry():
    async def check():
        publisher, emitter, repository = setup()
        await publisher.accepted()
        repository.fail = True
        waiting = asyncio.create_task(emitter.next_event(1))
        with pytest.raises(EventPersistenceError):
            await publisher.action(action(), task_id="t", goal_id="g")
        with pytest.raises(EventPersistenceError):
            await asyncio.wait_for(waiting, 1)
        assert [e.type for e in emitter.record.events] == ["accepted"]
        assert emitter.record.action_delivery == {}
        assert emitter.committed_seq == 1
        with pytest.raises(EventPersistenceError):
            await publisher.completed(outcome())
        assert len(repository.saved) == 1
        await emitter.finish()

    asyncio.run(check())


def test_cancelled_storage_write_does_not_expose_tentative_event():
    async def check():
        publisher, emitter, repository = setup()
        repository.gate = asyncio.Event()
        producer = asyncio.create_task(publisher.accepted())
        await repository.entered.wait()
        producer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await producer
        assert not emitter.record.events
        with pytest.raises(EventPersistenceError):
            await asyncio.wait_for(emitter.next_event(0), 1)
        await emitter.finish()

    asyncio.run(check())


def test_partial_multiframe_delivery_disconnect_keeps_unknown_and_can_complete():
    class TwoFrameAdapter(NativeOutputAdapter):
        def encode_event(self, event):
            return ("first", "second")

    async def check():
        publisher, emitter, repository = setup()
        await publisher.action(action(), task_id="t", goal_id="g")
        cancelled = []

        async def cancel():
            cancelled.append(True)
            emitter.record.status = "cancelled"
            await publisher.completed(
                RunOutcome(outcome="cancelled", goal_statuses={}, task_statuses={})
            )
            await emitter.finish()

        stream = EventDelivery(emitter, TwoFrameAdapter(), cancel).stream()
        assert await anext(stream) == "first"
        await stream.aclose()
        assert cancelled == [True]
        assert emitter.record.action_delivery == {"a": "delivery_unknown"}
        assert repository.saved[-1].events[-1].type == "completed"
        assert await emitter.next_event(0) is None

    asyncio.run(check())


def test_normal_delivery_marks_transport_and_snapshot_never_replays_actions():
    async def check():
        publisher, emitter, _ = setup()
        adapter = NativeOutputAdapter()
        await publisher.action(action(), task_id="t", goal_id="g")
        await publisher.completed(outcome())
        await emitter.finish()
        blocking = adapter.snapshot(emitter.record, purpose=SnapshotPurpose.BLOCKING)
        assert any(e["type"] == "action_ready" for e in blocking["events"])
        assert blocking["action_delivery"] == {"a": "delivery_unknown"}
        assert not blocking["reused"]

        async def cancel():
            pytest.fail("normal delivery must not cancel")

        frames = [frame async for frame in EventDelivery(emitter, adapter, cancel).stream()]
        assert len(frames) == 2 and "event: action_ready" in frames[0]
        assert emitter.record.action_delivery == {"a": "dispatched"}
        for purpose in (SnapshotPurpose.REUSED, SnapshotPurpose.STATUS, SnapshotPurpose.CANCEL):
            snapshot = adapter.snapshot(emitter.record, purpose=purpose)
            assert not any(e["type"] == "action_ready" for e in snapshot["events"])
        reused = EventEmitter(emitter.record, RecordingRepository(), streamable=False)
        assert await reused.next_event(0) is None

    asyncio.run(check())


@pytest.mark.parametrize("failed_kind", ["progress", "completed", "task_failed"])
def test_application_stops_on_event_storage_failure_and_cleans_active_run(monkeypatch, failed_kind):
    async def check():
        async with runtime() as (container, backend):
            if failed_kind == "task_failed":

                class BrokenRuntime:
                    async def execute(self, ctx):
                        raise DomainError("fixture", "测试领域失败。")

                container.application.agent_runtime = BrokenRuntime()
            original = container.repository.save_run
            failures = []

            async def fail_progress(record):
                if record.events and record.events[-1].type == failed_kind:
                    failures.append(True)
                    raise OSError("fixture")
                await original(record)

            monkeypatch.setattr(container.repository, "save_run", fail_progress)
            run = await asyncio.wait_for(container.application.execute(request("你好")), 1)
            assert run.record.status == "failed"
            assert run.emitter.finished and run.emitter.persistence_failed
            assert not container.application.active and not backend.calls
            assert failures == [True]
            assert run.record.events[0].type == "accepted"
            assert not any(e.type == failed_kind for e in run.record.events)

    asyncio.run(check())


def test_http_duplicate_streaming_and_status_preserve_action_delivery():
    async def check():
        async with runtime(scene_actions_enabled=True, service_bearer_token="test-token") as (
            c,
            backend,
        ):
            backend.overrides["navigate_scene"] = scene_response
            backend.gates["navigate_scene"] = asyncio.Event()
            app = create_app(c.settings)
            app.state.container = c
            body = {
                "query": "打开面板",
                "request_id": "one",
                "conversation_id": "conversation",
                "user": "actor",
                "response_mode": "blocking",
                "inputs": {
                    "tenantId": "tenant",
                    "patientId": "461",
                    "spaceId": "space",
                    "sceneVersion": 7,
                },
            }
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer test-token"},
            ) as client:
                first = asyncio.create_task(client.post("/v1/chat", json=body))
                await asyncio.wait_for(
                    backend.entered.setdefault("navigate_scene", asyncio.Event()).wait(), 1
                )
                repeat = {**body, "response_mode": "streaming"}
                running = await client.post("/v1/chat", json=repeat)
                assert (
                    running.status_code == 202
                    and "application/json" in running.headers["content-type"]
                )
                assert running.json()["reused"]
                backend.gates["navigate_scene"].set()
                initial = (await asyncio.wait_for(first, 1)).json()
                assert sum(e["type"] == "action_ready" for e in initial["events"]) == 1
                assert set(initial["action_delivery"].values()) == {"delivery_unknown"}
                again = await client.post("/v1/chat", json=repeat)
                assert again.status_code == 200 and again.json()["run_id"] == initial["run_id"]
                snapshots = [again.json()]
                for endpoint in ("status", "cancel"):
                    response = await client.post(
                        f"/v1/runs/{initial['run_id']}/{endpoint}",
                        json={"user": body["user"], "inputs": body["inputs"]},
                    )
                    assert response.status_code == 200
                    snapshots.append(response.json())
                assert all(
                    not any(e["type"] == "action_ready" for e in s["events"]) for s in snapshots
                )
                assert len(backend.calls) == 1

    asyncio.run(check())
