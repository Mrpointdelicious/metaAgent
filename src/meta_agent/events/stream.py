"""
创建日期：2026-09-08
文件功能：串行提交事件并按序通知消费者，独立管理运行结束与传输脱离。
"""

import asyncio
from copy import deepcopy
from typing import Any

from meta_agent.contracts import EventType, OutboundEvent, RunRecord, utcnow
from meta_agent.infrastructure.repository import Repository


class EventPersistenceError(RuntimeError):
    """An uncertain storage write stops event delivery and subsequent publication."""


class EventEmitter:
    def __init__(
        self, record: RunRecord, repository: Repository, *, streamable: bool = True
    ) -> None:
        self.record, self.repository = record, repository
        self.condition = asyncio.Condition()
        self.committed_seq = len(record.events)
        self.finished = not streamable or any(e.type == "completed" for e in record.events)
        self.detached = not streamable
        self.persistence_failed = False

    async def emit(
        self,
        kind: EventType,
        payload: dict[str, Any],
        *,
        task_id: str | None = None,
        goal_id: str | None = None,
    ) -> OutboundEvent:
        async with self.condition:
            if self.persistence_failed:
                raise EventPersistenceError("Event storage is unavailable")
            if self.finished or any(e.type == "completed" for e in self.record.events):
                raise RuntimeError("Event publication has finished")
            seq = self.committed_seq + 1
            if kind == "completed":
                payload = {**payload, "event_range": {"first_seq": 1, "last_seq": seq}}
            event = OutboundEvent(
                request_id=self.record.request_id,
                run_id=self.record.run_id,
                conversation_id=self.record.conversation_id,
                seq=seq,
                type=kind,
                payload=payload,
                task_id=task_id,
                goal_id=goal_id,
            )
            previous_delivery = dict(self.record.action_delivery)
            previous_timing = deepcopy(self.record.metrics.get("event_ready_ms"))
            if kind == "action_ready":
                self.record.action_delivery[payload["action_id"]] = "delivery_unknown"
            self.record.events.append(event)
            timing = self.record.metrics.setdefault("event_ready_ms", {})
            timing.setdefault(
                kind, (event.created_at - self.record.created_at).total_seconds() * 1000
            )
            try:
                await self._save()
            except BaseException:
                self.record.events.pop()
                self.record.action_delivery = previous_delivery
                if previous_timing is None:
                    self.record.metrics.pop("event_ready_ms", None)
                else:
                    self.record.metrics["event_ready_ms"] = previous_timing
                raise
            self.committed_seq = seq
            self.condition.notify_all()
            return event

    async def _save(self) -> None:
        # Call only while holding the condition. A cancelled write is also uncertain.
        try:
            await self.repository.save_run(self.record)
        except BaseException as exc:
            self.persistence_failed = True
            self.condition.notify_all()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise EventPersistenceError("Unable to persist event state") from exc

    async def next_event(self, after_seq: int) -> OutboundEvent | None:
        if after_seq < 0:
            raise ValueError("Event cursor must be nonnegative")
        async with self.condition:
            await self.condition.wait_for(
                lambda: (
                    self.persistence_failed
                    or self.detached
                    or self.finished
                    or self.committed_seq > after_seq
                )
            )
            if self.persistence_failed:
                raise EventPersistenceError("Event storage is unavailable")
            if self.detached:
                return None
            if after_seq < self.committed_seq:
                return self.record.events[after_seq].model_copy(deep=True)
            return None

    async def mark_dispatched(self, event: OutboundEvent) -> None:
        if event.type != "action_ready":
            return
        async with self.condition:
            if self.detached or self.persistence_failed:
                return
            # Dispatched only means handed to the response transport, not Unity execution.
            self.record.action_delivery[event.payload["action_id"]] = "dispatched"
            self.record.metrics.setdefault("action_transport_ms", {})[
                event.payload["action_id"]
            ] = (utcnow() - event.created_at).total_seconds() * 1000
            try:
                await self._save()
            except BaseException:
                self.record.action_delivery[event.payload["action_id"]] = "delivery_unknown"
                raise

    async def snapshot(self) -> RunRecord:
        async with self.condition:
            if self.persistence_failed:
                raise EventPersistenceError("Event storage is unavailable")
            return self.record.model_copy(deep=True)

    async def finish(self) -> None:
        async with self.condition:
            self.finished = True
            self.condition.notify_all()

    async def disconnect(self) -> None:
        async with self.condition:
            self.detached = True
            self.condition.notify_all()
            for action in self.record.action_delivery:
                self.record.action_delivery[action] = "delivery_unknown"
            if not self.persistence_failed:
                await self._save()
