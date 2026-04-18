from __future__ import annotations

from typing import Any

from config import Settings
from models import SessionIdentityContext
from repositories import RehabRepository


class UserLookupService:
    def __init__(self, repository: RehabRepository, settings: Settings | None = None):
        self.repository = repository
        self.settings = settings

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
        }

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
        }

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
