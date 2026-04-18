from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ResultSetArtifact(BaseModel):
    result_set_id: str
    result_set_type: str
    owner_scope: str
    count: int
    summary: str | None = None
    source_tool: str | None = None
    source_intent: str | None = None
    created_at: str | None = None
    expires_at: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)


class ActiveResultSetRef(BaseModel):
    result_set_id: str
    result_set_type: str
    count: int
    summary: str | None = None


class ThreadWorkingContext(BaseModel):
    session_id: str
    active_result_set_id: str | None = None
    active_result_set_type: str | None = None
    active_result_count: int | None = None
    last_result_summary: str | None = None
    default_time_window_days: int | None = None

    @property
    def active_result_set(self) -> ActiveResultSetRef | None:
        if not self.active_result_set_id or not self.active_result_set_type:
            return None
        return ActiveResultSetRef(
            result_set_id=self.active_result_set_id,
            result_set_type=self.active_result_set_type,
            count=int(self.active_result_count or 0),
            summary=self.last_result_summary,
        )
