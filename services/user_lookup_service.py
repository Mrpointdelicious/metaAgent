from __future__ import annotations

from typing import Any

from config import Settings
from models import SessionIdentityContext
from repositories import RehabRepository
from server.result_set_store import ResultSetStore


class UserLookupService:
    def __init__(self, repository: RehabRepository, settings: Settings | None = None, result_set_store: ResultSetStore | None = None):
        self.repository = repository
        self.settings = settings
        self.result_set_store = result_set_store

    def lookup_accessible_user_name(
        self,
        identity_context: SessionIdentityContext | None,
        user_id: int,
    ) -> dict[str, Any]:
        if identity_context is None:
            return self._inaccessible(user_id, reason="missing_identity_context")

        target_user_id = int(user_id)
        if identity_context.actor_role == "doctor":
            actor_doctor_id = identity_context.actor_doctor_id
            if actor_doctor_id is None:
                return self._inaccessible(target_user_id, reason="missing_actor_doctor_id")
            if target_user_id == int(actor_doctor_id):
                return self._accessible_user(target_user_id, user_role="doctor")

            related_patients = self.repository.get_related_patients_for_doctor(int(actor_doctor_id))
            patient_ids = {int(row["patient_id"]) for row in related_patients if row.get("patient_id") is not None}
            if target_user_id in patient_ids:
                return self._accessible_user(target_user_id, user_role="patient")
            return self._inaccessible(target_user_id, reason="not_accessible_or_not_found")

        actor_patient_id = identity_context.actor_patient_id
        if actor_patient_id is None:
            return self._inaccessible(target_user_id, reason="missing_actor_patient_id")
        if target_user_id == int(actor_patient_id):
            return self._accessible_user(target_user_id, user_role="patient")

        related_doctors = self.repository.get_related_doctors_for_patient(int(actor_patient_id))
        doctor_ids = {int(row["doctor_id"]) for row in related_doctors if row.get("doctor_id") is not None}
        if target_user_id in doctor_ids:
            return self._accessible_user(target_user_id, user_role="doctor")
        return self._inaccessible(target_user_id, reason="not_accessible_or_not_found")

    def list_my_patients(
        self,
        identity_context: SessionIdentityContext | None,
        *,
        days: int | None = None,
    ) -> dict[str, Any]:
        if identity_context is None or identity_context.actor_role != "doctor" or identity_context.actor_doctor_id is None:
            return {
                "is_accessible": False,
                "count": 0,
                "rows": [],
                "reason": "doctor_identity_required",
            }
        rows = self.repository.get_related_patients_for_doctor(
            int(identity_context.actor_doctor_id),
            days=days,
        )
        normalized_rows = [
            {
                "patient_id": int(row["patient_id"]),
                "patient_name": row.get("patient_name"),
            }
            for row in rows
            if row.get("patient_id") is not None
        ]
        return {
            "is_accessible": True,
            "count": len(normalized_rows),
            "rows": normalized_rows,
            "days": days,
        } | self._register_collection_result(
            identity_context,
            rows=normalized_rows,
            result_set_type="patient_set",
            summary=f"list_my_patients returned {len(normalized_rows)} rows.",
            source_tool="list_my_patients",
            source_intent="lookup_query",
            days=days,
        )

    def list_my_doctors(
        self,
        identity_context: SessionIdentityContext | None,
        *,
        days: int | None = None,
    ) -> dict[str, Any]:
        if identity_context is None or identity_context.actor_role != "patient" or identity_context.actor_patient_id is None:
            return {
                "is_accessible": False,
                "count": 0,
                "rows": [],
                "reason": "patient_identity_required",
            }
        rows = self.repository.get_related_doctors_for_patient(
            int(identity_context.actor_patient_id),
            days=days,
        )
        normalized_rows = [
            {
                "doctor_id": int(row["doctor_id"]),
                "doctor_name": row.get("doctor_name"),
            }
            for row in rows
            if row.get("doctor_id") is not None
        ]
        return {
            "is_accessible": True,
            "count": len(normalized_rows),
            "rows": normalized_rows,
            "days": days,
        } | self._register_collection_result(
            identity_context,
            rows=normalized_rows,
            result_set_type="doctor_set",
            summary=f"list_my_doctors returned {len(normalized_rows)} rows.",
            source_tool="list_my_doctors",
            source_intent="lookup_query",
            days=days,
        )

    def _accessible_user(self, user_id: int, *, user_role: str) -> dict[str, Any]:
        user_name = self.repository.get_user_name_map([user_id]).get(user_id)
        return {
            "user_id": user_id,
            "user_name": user_name,
            "user_role": user_role,
            "is_accessible": True,
            "found": user_name is not None,
        }

    def _inaccessible(self, user_id: int, *, reason: str) -> dict[str, Any]:
        return {
            "user_id": user_id,
            "user_name": None,
            "user_role": None,
            "is_accessible": False,
            "found": False,
            "reason": reason,
        }

    def _register_collection_result(
        self,
        identity_context: SessionIdentityContext,
        *,
        rows: list[dict[str, Any]],
        result_set_type: str,
        summary: str,
        source_tool: str,
        source_intent: str,
        days: int | None,
    ) -> dict[str, Any]:
        if self.result_set_store is None:
            return {}
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
            "result_set_id": artifact.result_set_id,
            "result_set_type": artifact.result_set_type,
            "active_result_set": artifact.model_dump(mode="json", exclude={"rows"}),
        }
