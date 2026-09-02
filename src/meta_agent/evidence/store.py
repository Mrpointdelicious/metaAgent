"""
创建日期：2026-08-29
文件功能：封装 LangGraph Store，按作用域保存和读取完整证据结果。
"""

from datetime import UTC, datetime
from typing import Any

from langgraph.store.base import BaseStore

from meta_agent.evidence.models import EvidenceRecord


class EvidenceRepository:
    """通过命名空间强制执行患者级证据隔离。"""

    def __init__(self, store: BaseStore, ttl_seconds: int) -> None:
        self._store = store
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _namespace(scope_hash: str) -> tuple[str, ...]:
        return ("evidence", scope_hash)

    async def save(
        self,
        scope_hash: str,
        source: str,
        payload: dict[str, Any],
    ) -> EvidenceRecord:
        record = EvidenceRecord.create(
            scope_hash=scope_hash,
            source=source,
            payload=payload,
            ttl_seconds=self._ttl_seconds,
        )
        await self._store.aput(
            self._namespace(scope_hash),
            record.result_ref,
            record.model_dump(mode="json"),
        )
        return record

    async def get(self, scope_hash: str, result_ref: str) -> EvidenceRecord | None:
        item = await self._store.aget(self._namespace(scope_hash), result_ref)
        if item is None:
            return None
        record = EvidenceRecord.model_validate(item.value)
        if record.scope_hash != scope_hash or record.expires_at <= datetime.now(UTC):
            return None
        return record
