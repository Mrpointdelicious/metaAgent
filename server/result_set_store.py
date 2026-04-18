from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from config import Settings, get_settings
from models import ActiveResultSetRef, ResultSetArtifact, SessionIdentityContext, ThreadWorkingContext


class ResultSetStore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._artifacts: dict[str, ResultSetArtifact] = {}
        self._thread_state: dict[str, ThreadWorkingContext] = {}
        self._lock = threading.RLock()

    def owner_scope_for_identity(self, identity_context: SessionIdentityContext | None) -> str:
        if identity_context is None:
            return "anonymous"
        tenant = identity_context.tenant_id or "default"
        if identity_context.actor_role == "doctor":
            return f"{tenant}:doctor:{identity_context.actor_doctor_id}"
        return f"{tenant}:patient:{identity_context.actor_patient_id}"

    def thread_key_for_identity(self, identity_context: SessionIdentityContext | None) -> str | None:
        if identity_context is None or not identity_context.session_id:
            return None
        return str(identity_context.session_id)

    def get_thread_context(self, identity_context: SessionIdentityContext | None) -> ThreadWorkingContext | None:
        thread_key = self.thread_key_for_identity(identity_context)
        if thread_key is None:
            return None
        with self._lock:
            context = self._thread_state.get(thread_key)
            if context is None:
                return None
            active = context.active_result_set
            if active is not None and self._artifact_expired(active.result_set_id):
                self._thread_state.pop(thread_key, None)
                return None
            return context.model_copy(deep=True)

    def get_active_ref(self, identity_context: SessionIdentityContext | None) -> ActiveResultSetRef | None:
        context = self.get_thread_context(identity_context)
        if context is None:
            return None
        active = context.active_result_set
        if active is None:
            return None
        try:
            self.get_artifact(active.result_set_id, identity_context)
        except (KeyError, PermissionError):
            return None
        return active

    def register_result_set(
        self,
        *,
        identity_context: SessionIdentityContext,
        rows: list[dict[str, Any]],
        result_set_type: str,
        summary: str | None,
        source_tool: str,
        source_intent: str,
        ttl_seconds: int | None = None,
    ) -> ResultSetArtifact:
        thread_key = self.thread_key_for_identity(identity_context)
        if thread_key is None:
            raise ValueError("result_set.session_id_required")

        now = datetime.now(timezone.utc)
        ttl = ttl_seconds if ttl_seconds is not None else self.settings.result_set_ttl_seconds
        expires_at = now + timedelta(seconds=int(ttl)) if ttl else None
        artifact = ResultSetArtifact(
            result_set_id=f"rs_{uuid4().hex}",
            result_set_type=result_set_type,
            owner_scope=self.owner_scope_for_identity(identity_context),
            count=len(rows),
            summary=summary,
            source_tool=source_tool,
            source_intent=source_intent,
            created_at=now.isoformat(),
            expires_at=expires_at.isoformat() if expires_at else None,
            rows=[dict(row) for row in rows],
        )
        state = ThreadWorkingContext(
            session_id=thread_key,
            active_result_set_id=artifact.result_set_id,
            active_result_set_type=artifact.result_set_type,
            active_result_count=artifact.count,
            last_result_summary=artifact.summary,
            default_time_window_days=self.settings.default_time_window_days,
        )
        with self._lock:
            self._artifacts[artifact.result_set_id] = artifact
            self._thread_state[thread_key] = state
        return artifact.model_copy(deep=True)

    def get_artifact(
        self,
        result_set_id: str,
        identity_context: SessionIdentityContext | None,
    ) -> ResultSetArtifact:
        with self._lock:
            artifact = self._artifacts.get(result_set_id)
            if artifact is None or self._artifact_expired(result_set_id):
                self._artifacts.pop(result_set_id, None)
                raise KeyError(result_set_id)
            expected_scope = self.owner_scope_for_identity(identity_context)
            if artifact.owner_scope != expected_scope:
                raise PermissionError("result_set.owner_scope_mismatch")
            return artifact.model_copy(deep=True)

    def apply_to_context(
        self,
        identity_context: SessionIdentityContext | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        merged = dict(context or {})
        thread_context = self.get_thread_context(identity_context)
        if thread_context is None:
            merged.setdefault("default_time_window_days", self.settings.default_time_window_days)
            return merged
        merged["thread_state"] = thread_context.model_dump(mode="json")
        merged["default_time_window_days"] = thread_context.default_time_window_days
        active = thread_context.active_result_set
        if active is not None:
            merged["active_result_set"] = active.model_dump(mode="json")
            merged["active_result_set_id"] = active.result_set_id
            merged["active_result_set_type"] = active.result_set_type
            merged["active_result_count"] = active.count
            merged["last_result_summary"] = active.summary
        return merged

    def clear(self) -> None:
        with self._lock:
            self._artifacts.clear()
            self._thread_state.clear()

    def _artifact_expired(self, result_set_id: str) -> bool:
        artifact = self._artifacts.get(result_set_id)
        if artifact is None or not artifact.expires_at:
            return artifact is None
        try:
            expires_at = datetime.fromisoformat(artifact.expires_at)
        except ValueError:
            return False
        return datetime.now(timezone.utc) >= expires_at


_DEFAULT_RESULT_SET_STORE = ResultSetStore()


def get_result_set_store() -> ResultSetStore:
    return _DEFAULT_RESULT_SET_STORE
