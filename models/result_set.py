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
    """Minimal single-active-result-set state for one conversation thread.

    The active result set is a short reference to a reusable collection
    artifact. Code updates this state only through ResultSetStore.
    """

    thread_id: str | None = Field(default=None, description="Store key for this thread; conversation_id when available, otherwise session_id.")
    session_id: str | None = Field(default=None, description="Frontend/session identifier kept for traceability.")
    conversation_id: str | None = Field(default=None, description="Conversation/thread identifier used for isolation when available.")
    active_result_set_id: str | None = Field(default=None, description="Single active reusable result set ID for this thread.")
    active_result_set_type: str | None = Field(default=None, description="Type of the active result set, such as patient_set or doctor_set.")
    active_result_count: int | None = Field(default=None, description="Number of rows in the active result set.")
    last_result_summary: str | None = Field(default=None, description="Human-readable summary of the active result set.")
    default_time_window_days: int | None = Field(default=None, description="Explicitly selected default relative window for follow-up result-set tools.")

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
