"""
创建日期：2026-09-12
文件功能：保留未来 Dify 事件适配器边界，不参与当前运行链。
"""

from typing import Any

from meta_agent.contracts import OutboundEvent, RunRecord
from meta_agent.events.native import SnapshotPurpose


class DifyEventAdapter:
    supports_actions = False

    def encode_event(self, event: OutboundEvent) -> tuple[str, ...]:
        raise NotImplementedError("Dify protocol mapping is deferred; use NativeOutputAdapter")

    def snapshot(self, record: RunRecord, *, purpose: SnapshotPurpose) -> dict[str, Any]:
        raise NotImplementedError("Dify protocol mapping is deferred; use NativeOutputAdapter")
