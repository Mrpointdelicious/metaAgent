"""
创建日期：2026-10-10
文件功能：将内部事件投影为前端Run协议，按序回放并保持订阅独立于运行生命周期。
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from meta_agent.contracts import OutboundEvent, RunRecord
from meta_agent.events.stream import EventEmitter


def public_outcome(status: str) -> str:
    return "unavailable" if status in {"blocked_dependency", "skipped_condition"} else status


class AgentOutputAdapter:
    supports_actions = True

    def encode_event(self, event: OutboundEvent) -> tuple[str, ...]:
        payload = dict(event.payload)
        if event.type == "progress":
            payload["stage"] = {"agent": "planning", "tool": "querying"}.get(
                payload["stage"], payload["stage"]
            )
        elif event.type == "completed":
            payload["outcome"] = public_outcome(payload["outcome"])
        elif event.type == "action_ready":
            payload.pop("source_evidence_id", None)
        data = {
            "event_id": event.event_id,
            "request_id": event.request_id,
            "run_id": event.run_id,
            "conversation_id": event.conversation_id,
            "seq": event.seq,
            "task_id": event.task_id,
            "goal_id": event.goal_id,
            "type": event.type,
            "payload": payload,
            "created_at": event.created_at.isoformat(),
        }
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return (f"id: {event.seq}\nevent: {event.type}\ndata: {encoded}\n\n",)

    def snapshot(self, record: RunRecord) -> dict[str, Any]:
        return {
            "request_id": record.request_id,
            "run_id": record.run_id,
            "conversation_id": record.conversation_id,
            "status": public_outcome(record.status),
            "last_seq": len(record.events),
            "created_at": record.created_at.isoformat(),
        }


class RunSubscription:
    def __init__(self, emitter: EventEmitter, adapter: AgentOutputAdapter, after_seq: int):
        self.emitter, self.adapter, self.after_seq = emitter, adapter, after_seq

    async def stream(self) -> AsyncIterator[str]:
        cursor = self.after_seq
        while True:
            try:
                event = await asyncio.wait_for(self.emitter.next_event(cursor), timeout=15)
            except TimeoutError:
                yield ": heartbeat\n\n"
                continue
            if event is None:
                return
            for frame in self.adapter.encode_event(event):
                yield frame
            await self.emitter.mark_dispatched(event)
            cursor = event.seq
            if event.type == "completed":
                return
