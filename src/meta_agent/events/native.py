"""
创建日期：2026-10-09
文件功能：定义输出适配契约并统一原生SSE、首次阻塞与只读快照投影。
"""

import json
from copy import deepcopy
from enum import StrEnum
from typing import Any, Protocol

from meta_agent.contracts import OutboundEvent, RunRecord


class SnapshotPurpose(StrEnum):
    BLOCKING = "blocking"
    REUSED = "reused"
    STATUS = "status"
    CANCEL = "cancel"


class OutputAdapter(Protocol):
    supports_actions: bool

    def encode_event(self, event: OutboundEvent) -> tuple[str, ...]: ...

    def snapshot(self, record: RunRecord, *, purpose: SnapshotPurpose) -> dict[str, Any]: ...


class NativeOutputAdapter:
    supports_actions = True

    def encode_event(self, event: OutboundEvent) -> tuple[str, ...]:
        data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        return (f"id: {event.event_id}\nevent: {event.type}\ndata: {data}\n\n",)

    def snapshot(self, record: RunRecord, *, purpose: SnapshotPurpose) -> dict[str, Any]:
        blocking = purpose == SnapshotPurpose.BLOCKING
        metrics = deepcopy(record.metrics)
        # Also redact this retired internal field in pre-migration records.
        metrics.pop("patient_profile_context", None)
        return {
            "run_id": record.run_id,
            "request_id": record.request_id,
            "conversation_id": record.conversation_id,
            "status": record.status,
            "reused": not blocking,
            "goal_statuses": dict(record.goal_statuses),
            "task_statuses": {tid: result.status for tid, result in record.results.items()},
            "events": [
                e.model_dump(mode="json")
                for e in record.events
                if blocking or e.type != "action_ready"
            ],
            "action_delivery": dict(record.action_delivery),
            "metrics": metrics,
        }
