"""
创建日期：2026-09-08
文件功能：单一事件发送器分配序号，先登记后交付，保留动作交付不确定状态。
"""

import asyncio
import json
from typing import Any, Protocol

from meta_agent.contracts import EventType, OutboundEvent, RunRecord
from meta_agent.infrastructure.repository import Repository


class EventAdapter(Protocol):
    """第三方适配层只消费事件，不参与任务或命令生成。"""
    def encode(self, event: OutboundEvent) -> str: ...


class NativeEventAdapter:
    def encode(self, event: OutboundEvent) -> str:
        data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        return f"id: {event.event_id}\nevent: {event.type}\ndata: {data}\n\n"


class EventEmitter:
    def __init__(self, record: RunRecord, repository: Repository) -> None:
        self.record, self.repository = record, repository
        self.queue: asyncio.Queue[OutboundEvent | None] = asyncio.Queue(maxsize=128)
        self.lock = asyncio.Lock()
        self.closed = False

    async def emit(self, kind: EventType, payload: dict[str, Any], *, task_id: str | None = None,
                   goal_id: str | None = None) -> OutboundEvent:
        async with self.lock:
            if self.closed:
                raise asyncio.CancelledError
            seq = len(self.record.events) + 1
            if kind == "completed":
                payload = {**payload, "event_range": {"first_seq": 1, "last_seq": seq}}
            event = OutboundEvent(request_id=self.record.request_id, run_id=self.record.run_id,
                conversation_id=self.record.conversation_id, seq=seq, type=kind, payload=payload,
                task_id=task_id, goal_id=goal_id)
            if kind == "action_ready":
                self.record.action_delivery[payload["action_id"]] = "delivery_unknown"
            self.record.events.append(event)
            await self.repository.save_run(self.record)
            await self.queue.put(event)
            return event

    async def mark_dispatched(self, event: OutboundEvent) -> None:
        if event.type != "action_ready":
            return
        async with self.lock:
            # Dispatched only means handed to the response transport, not Unity execution.
            self.record.action_delivery[event.payload["action_id"]] = "dispatched"
            await self.repository.save_run(self.record)

    async def finish(self) -> None:
        await self.queue.put(None)

    async def disconnect(self) -> None:
        async with self.lock:
            self.closed = True
            for action in self.record.action_delivery:
                self.record.action_delivery[action] = "delivery_unknown"
            await self.repository.save_run(self.record)
