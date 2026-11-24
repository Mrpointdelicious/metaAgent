"""
创建日期：2026-11-17
文件功能：定义患者偏好读写协议和开发用内存实现，按账号与偏好类型去重。
"""

import re
from dataclasses import replace
from typing import Protocol
from uuid import UUID

from meta_agent.contracts import utcnow
from meta_agent.infrastructure.locks import ThreadLockRegistry
from meta_agent.memory.schemas import (
    MemoryArgs,
    MemoryChange,
    PreferenceError,
    PreferenceRecord,
    PreferenceSnapshot,
)


def validate_owner(owner: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", owner):
        raise PreferenceError("invalid_memory_owner")


def decide_change(items, args: MemoryArgs, generated_id: UUID):
    """Decide under the owner lock; create becomes an upsert by preference key."""
    old = next((item for item in items if item.id == args.id), None)
    if args.action == "create":
        old = next((item for item in items if item.content.key == args.content.key), None)
    elif old is None:
        raise PreferenceError("memory_not_found")
    if old:
        if old.content.key != args.content.key:
            raise PreferenceError("memory_key_mismatch")
        return ("unchanged" if old.content == args.content else "updated"), old.id
    return "created", generated_id


class PreferenceRepository(Protocol):
    async def snapshot(self, owner: str) -> PreferenceSnapshot: ...

    async def apply(self, owner: str, args: MemoryArgs, generated_id: UUID) -> MemoryChange: ...


class InMemoryPreferenceRepository:
    """Development only; production persists preferences in PostgreSQL."""

    def __init__(self):
        self._items: dict[str, dict[UUID, PreferenceRecord]] = {}
        self._locks = ThreadLockRegistry()

    async def snapshot(self, owner: str) -> PreferenceSnapshot:
        validate_owner(owner)
        async with self._locks.hold(owner):
            return PreferenceSnapshot(
                tuple(sorted(self._items.get(owner, {}).values(), key=lambda i: i.content.key)),
            )

    async def apply(self, owner: str, args: MemoryArgs, generated_id: UUID) -> MemoryChange:
        validate_owner(owner)
        args = MemoryArgs.model_validate(args.model_dump())
        async with self._locks.hold(owner):
            items = self._items.setdefault(owner, {})
            status, memory_id = decide_change(items.values(), args, generated_id)
            if status == "created":
                now = utcnow()
                items[memory_id] = PreferenceRecord(memory_id, args.content, now, now)
            elif status == "updated":
                items[memory_id] = replace(
                    items[memory_id], content=args.content, updated_at=utcnow()
                )
            return MemoryChange("saved", memory_id)
