from __future__ import annotations

from typing import Any

from models import ResultSetArtifact, SessionIdentityContext
from repositories import RehabRepository
from server.result_set_store import ResultSetStore

from .shared import build_time_range, parse_datetime_flexible


class ResultSetService:
    def __init__(self, repository: RehabRepository, result_set_store: ResultSetStore):
        self.repository = repository
        self.result_set_store = result_set_store

    def filter_result_set_by_training(
        self,
        identity_context: SessionIdentityContext | None,
        *,
        result_set_id: str,
        days: int | None = None,
    ) -> dict[str, Any]:
        artifact, error = self._load_patient_set(identity_context, result_set_id)
        if error:
            return error
        assert artifact is not None
        if days is None:
            return self._error("result_set.days_required")
        start, end = self._window(identity_context, days)
        doctor_id = self._doctor_id(identity_context)
        rows: list[dict[str, Any]] = []
        for row in artifact.rows:
            patient_id = row.get("patient_id")
            if patient_id is None:
                continue
            executions = self.repository.get_execution_logs(
                patient_id=int(patient_id),
                therapist_id=doctor_id,
                start=start,
                end=end,
                limit=2000,
            )
            if executions:
                item = dict(row)
                item["training_count_in_window"] = len(executions)
                item["last_training_time"] = self._latest_time(executions, "StartTime")
                rows.append(item)
        return self._register_and_payload(
            identity_context,
            rows=rows,
            result_set_type=artifact.result_set_type,
            summary=f"{len(rows)} patients had training in the last {days} days.",
            source_tool="filter_result_set_by_training",
            source_intent="result_set_query",
            source_result_set_id=result_set_id,
            days=days,
        )

    def filter_result_set_by_absence(
        self,
        identity_context: SessionIdentityContext | None,
        *,
        result_set_id: str,
        days: int | None = None,
    ) -> dict[str, Any]:
        artifact, error = self._load_patient_set(identity_context, result_set_id)
        if error:
            return error
        assert artifact is not None
        if days is None:
            return self._error("result_set.days_required")
        start, end = self._window(identity_context, days)
        doctor_id = self._doctor_id(identity_context)
        rows: list[dict[str, Any]] = []
        for row in artifact.rows:
            patient_id = row.get("patient_id")
            if patient_id is None:
                continue
            executions = self.repository.get_execution_logs(
                patient_id=int(patient_id),
                therapist_id=doctor_id,
                start=start,
                end=end,
                limit=2000,
            )
            if not executions:
                item = dict(row)
                item["absence_window_days"] = days
                item["absence_reason"] = "no_training_logs_in_window"
                rows.append(item)
        return self._register_and_payload(
            identity_context,
            rows=rows,
            result_set_type=artifact.result_set_type,
            summary=f"{len(rows)} patients had no training logs in the last {days} days.",
            source_tool="filter_result_set_by_absence",
            source_intent="result_set_query",
            source_result_set_id=result_set_id,
            days=days,
        )

    def filter_result_set_by_plan_completion(
        self,
        identity_context: SessionIdentityContext | None,
        *,
        result_set_id: str,
        days: int | None = None,
    ) -> dict[str, Any]:
        artifact, error = self._load_patient_set(identity_context, result_set_id)
        if error:
            return error
        assert artifact is not None
        if days is None:
            return self._error("result_set.days_required")
        start, end = self._window(identity_context, days)
        doctor_id = self._doctor_id(identity_context)
        rows: list[dict[str, Any]] = []
        for row in artifact.rows:
            patient_id = row.get("patient_id")
            if patient_id is None:
                continue
            completed = self._completed_plan_rows(
                patient_id=int(patient_id),
                doctor_id=doctor_id,
                start=start,
                end=end,
            )
            if completed:
                item = dict(row)
                item["completed_plan_count_in_window"] = len(completed)
                item["completion_time"] = self._latest_completion_time(completed)
                rows.append(item)
        return self._register_and_payload(
            identity_context,
            rows=rows,
            result_set_type=artifact.result_set_type,
            summary=f"{len(rows)} patients completed plans in the last {days} days.",
            source_tool="filter_result_set_by_plan_completion",
            source_intent="result_set_query",
            source_result_set_id=result_set_id,
            days=days,
        )

    def enrich_result_set_with_completion_time(
        self,
        identity_context: SessionIdentityContext | None,
        *,
        result_set_id: str,
    ) -> dict[str, Any]:
        artifact, error = self._load_patient_set(identity_context, result_set_id)
        if error:
            return error
        assert artifact is not None
        doctor_id = self._doctor_id(identity_context)
        rows: list[dict[str, Any]] = []
        for row in artifact.rows:
            patient_id = row.get("patient_id")
            if patient_id is None:
                continue
            completed = self._completed_plan_rows(
                patient_id=int(patient_id),
                doctor_id=doctor_id,
                start=None,
                end=None,
            )
            item = dict(row)
            item["completion_time"] = self._latest_completion_time(completed)
            item["completed_plan_count"] = len(completed)
            rows.append(item)
        return self._register_and_payload(
            identity_context,
            rows=rows,
            result_set_type=artifact.result_set_type,
            summary=f"Added completion_time to {len(rows)} patients.",
            source_tool="enrich_result_set_with_completion_time",
            source_intent="result_set_query",
            source_result_set_id=result_set_id,
            days=None,
        )

    def _load_patient_set(
        self,
        identity_context: SessionIdentityContext | None,
        result_set_id: str,
    ) -> tuple[ResultSetArtifact | None, dict[str, Any] | None]:
        if identity_context is None:
            return None, self._error("missing_identity_context")
        try:
            artifact = self.result_set_store.get_artifact(result_set_id, identity_context)
        except KeyError:
            return None, self._error("result_set.not_found")
        except PermissionError:
            return None, self._error("result_set.owner_scope_mismatch")
        if artifact.result_set_type != "patient_set":
            return None, self._error("result_set.patient_set_required")
        return artifact, None

    def _register_and_payload(
        self,
        identity_context: SessionIdentityContext | None,
        *,
        rows: list[dict[str, Any]],
        result_set_type: str,
        summary: str,
        source_tool: str,
        source_intent: str,
        source_result_set_id: str,
        days: int | None,
    ) -> dict[str, Any]:
        if identity_context is None:
            return self._error("missing_identity_context")
        artifact = self.result_set_store.register_result_set(
            identity_context=identity_context,
            rows=rows,
            result_set_type=result_set_type,
            summary=summary,
            source_tool=source_tool,
            source_intent=source_intent,
            default_time_window_days=days,
        )
        return {
            "is_accessible": True,
            "source_tool": source_tool,
            "source_result_set_id": source_result_set_id,
            "result_set_id": artifact.result_set_id,
            "result_set_type": artifact.result_set_type,
            "active_result_set": artifact.model_dump(mode="json", exclude={"rows"}),
            "count": artifact.count,
            "rows": artifact.rows,
            "summary": summary,
            "days": days,
        }

    def _window(self, identity_context: SessionIdentityContext | None, days: int) -> tuple[Any, Any]:
        patient_id = identity_context.actor_patient_id if identity_context and identity_context.actor_role == "patient" else None
        doctor_id = self._doctor_id(identity_context)
        time_range = build_time_range(
            self.repository,
            patient_id=patient_id,
            therapist_id=doctor_id,
            days=int(days),
        )
        return time_range.start, time_range.end

    def _doctor_id(self, identity_context: SessionIdentityContext | None) -> int | None:
        if identity_context is None:
            return None
        if identity_context.actor_role == "doctor":
            return identity_context.actor_doctor_id
        return identity_context.target_doctor_id

    def _completed_plan_rows(
        self,
        *,
        patient_id: int,
        doctor_id: int | None,
        start: Any,
        end: Any,
    ) -> list[dict[str, Any]]:
        rows = self.repository.get_plan_records(
            patient_id=patient_id,
            therapist_id=doctor_id,
            start=start,
            end=end,
            limit=1000,
        )
        completed: list[dict[str, Any]] = []
        for row in rows:
            if row.get("IsComplete") == 1 or row.get("Status") == 1 or parse_datetime_flexible(row.get("Endtime")) is not None:
                completed.append(row)
        return completed

    def _latest_time(self, rows: list[dict[str, Any]], key: str) -> str | None:
        values = [parse_datetime_flexible(row.get(key)) for row in rows]
        values = [value for value in values if value is not None]
        return max(values).isoformat(sep=" ") if values else None

    def _latest_completion_time(self, rows: list[dict[str, Any]]) -> str | None:
        values = []
        for row in rows:
            for key in ("Endtime", "Updatetime", "BookingTime", "CreateTime"):
                value = parse_datetime_flexible(row.get(key))
                if value is not None:
                    values.append(value)
                    break
        return max(values).isoformat(sep=" ") if values else None

    def _error(self, reason: str) -> dict[str, Any]:
        return {
            "is_accessible": False,
            "count": 0,
            "rows": [],
            "reason": reason,
        }
