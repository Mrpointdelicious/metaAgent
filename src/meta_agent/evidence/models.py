"""
创建日期：2026-08-29
文件功能：定义上下文外证据记录及其访问范围、版本和过期信息。
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class EvidenceRecord(BaseModel):
    """保存于 Store、不会直接进入模型上下文的完整工具结果。"""

    result_ref: str = Field(default_factory=lambda: f"result_{uuid4().hex}")
    scope_hash: str
    source: str
    schema_version: str = "1.0"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    payload: dict[str, Any]

    @classmethod
    def create(
        cls,
        scope_hash: str,
        source: str,
        payload: dict[str, Any],
        ttl_seconds: int,
    ) -> "EvidenceRecord":
        now = datetime.now(UTC)
        return cls(
            scope_hash=scope_hash,
            source=source,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            payload=payload,
        )
