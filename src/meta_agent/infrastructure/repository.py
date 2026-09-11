"""
创建日期：2026-09-08
文件功能：按作用域持久化原始证据、会话、运行及缓存，并执行物理TTL清理。
"""

from datetime import datetime, timedelta
from typing import Any

from langgraph.store.base import BaseStore
from pydantic import Field

from meta_agent.config import Settings
from meta_agent.contracts import (
    ConversationState, FactView, RunRecord, StrictModel, fingerprint, identifier, utcnow,
)


class EvidenceEnvelope(StrictModel):
    evidence_id: str = Field(default_factory=lambda: identifier("evidence"))
    scope_key: str
    tool_name: str
    request_id: str
    contract_version: str
    source_version: str
    fetched_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    raw_payload_hash: str
    payload: dict[str, Any]


def resolve_pointer(payload: Any, pointer: str) -> tuple[bool, Any]:
    current = payload
    try:
        for part in pointer.lstrip("/").split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            current = current[int(part)] if isinstance(current, list) else current[part]
        return True, current
    except (KeyError, IndexError, ValueError, TypeError):
        return False, None


class Repository:
    ROOT = "metaagent-v1"

    def __init__(self, store: BaseStore, settings: Settings, checkpointer: Any = None) -> None:
        self.store, self.settings, self.checkpointer = store, settings, checkpointer

    def namespace(self, kind: str, scope: str) -> tuple[str, ...]:
        return (self.ROOT, kind, scope)

    async def put(self, kind: str, scope: str, key: str, value: dict[str, Any], ttl: int) -> None:
        await self.store.aput(self.namespace(kind, scope), key,
                              {"expires_at": utcnow().timestamp() + ttl, "value": value})

    async def get(self, kind: str, scope: str, key: str) -> dict[str, Any] | None:
        item = await self.store.aget(self.namespace(kind, scope), key)
        if item is None:
            return None
        if item.value["expires_at"] <= utcnow().timestamp():
            await self.delete(kind, scope, key)
            return None
        return item.value["value"]

    async def delete(self, kind: str, scope: str, key: str) -> None:
        if kind == "runs" and self.checkpointer is not None:
            await self.checkpointer.adelete_thread(key)
        await self.store.adelete(self.namespace(kind, scope), key)

    async def save_evidence(self, scope: str, tool: str, request_id: str,
                            payload: dict[str, Any], version: str) -> EvidenceEnvelope:
        digest = fingerprint(payload)
        meta = payload.get("meta") or {}
        watermark = meta.get("watermark") or digest
        evidence = EvidenceEnvelope(scope_key=scope, tool_name=tool, request_id=request_id,
                                    contract_version=version, source_version=f"{version}:{watermark}",
                                    expires_at=utcnow() + timedelta(seconds=self.settings.evidence_ttl_seconds),
                                    raw_payload_hash=digest, payload=payload)
        await self.put("evidence", scope, evidence.evidence_id, evidence.model_dump(mode="json"),
                       self.settings.evidence_ttl_seconds)
        return evidence

    async def evidence(self, scope: str, evidence_id: str) -> EvidenceEnvelope | None:
        value = await self.get("evidence", scope, evidence_id)
        if not value:
            return None
        evidence = EvidenceEnvelope.model_validate(value)
        if evidence.scope_key != scope or evidence.expires_at <= utcnow():
            return None
        return evidence

    async def verify_fact(self, scope: str, view: FactView) -> bool:
        fact = view.fact
        evidence = await self.evidence(scope, fact.evidence_id)
        if evidence is None or fact.source_version != evidence.source_version:
            return False
        exists, value = resolve_pointer(evidence.payload, fact.path)
        if not exists:
            return fact.value is None and fact.value_status == "missing"
        return type(value) is type(fact.value) and value == fact.value

    async def conversation(self, thread_id: str) -> ConversationState:
        data = await self.get("conversations", thread_id, "state")
        return ConversationState.model_validate(data) if data else ConversationState()

    async def save_conversation(self, thread_id: str, memory: ConversationState) -> None:
        memory.updated_at = utcnow()
        # A turn may contain a large answer; retention never stores full tool JSON.
        memory.turns = [{"user": t.get("user", "")[:2000], "assistant": t.get("assistant", "")[:2000]}
                        for t in memory.turns[-6:]]
        await self.put("conversations", thread_id, "state", memory.model_dump(mode="json"),
                       self.settings.conversation_ttl_seconds)

    async def save_run(self, record: RunRecord) -> None:
        await self.put("runs", record.scope_key, record.run_id, record.model_dump(mode="json"),
                       self.settings.run_ttl_seconds)

    async def run(self, scope: str, run_id: str) -> RunRecord | None:
        value = await self.get("runs", scope, run_id)
        return RunRecord.model_validate(value) if value else None

    async def request_run(self, scope: str, request_id: str) -> RunRecord | None:
        mapping = await self.get("requests", scope, fingerprint(request_id))
        return await self.run(scope, mapping["run_id"]) if mapping else None

    async def map_request(self, record: RunRecord) -> None:
        await self.put("requests", record.scope_key, fingerprint(record.request_id),
                       {"run_id": record.run_id}, self.settings.run_ttl_seconds)

    async def cleanup(self) -> int:
        count = 0
        # Always delete the first expired page. Offset pagination while deleting skips rows.
        while True:
            expired = await self.store.asearch((self.ROOT,),
                        filter={"expires_at": {"$lte": utcnow().timestamp()}}, limit=100)
            if not expired:
                return count
            for item in expired:
                await self.delete(item.namespace[1], item.namespace[2], item.key)
                count += 1

    async def ready(self) -> bool:
        await self.store.asearch((self.ROOT,), limit=1)
        return True
