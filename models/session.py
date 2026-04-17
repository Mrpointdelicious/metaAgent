from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SessionIdentityContext(BaseModel):
    actor_role: Literal["doctor", "patient"] = Field(description="Authenticated session actor role.")
    actor_doctor_id: int | None = Field(default=None, description="Doctor ID when the actor is a doctor.")
    actor_patient_id: int | None = Field(default=None, description="Patient ID when the actor is a patient.")

    target_doctor_id: int | None = Field(default=None, description="Default doctor scope for this session.")
    target_patient_id: int | None = Field(default=None, description="Default patient target for this session.")

    tenant_id: str | None = Field(default=None, description="Reserved tenant boundary.")
    org_id: str | None = Field(default=None, description="Reserved organization boundary.")
    session_id: str | None = Field(default=None, description="External session identifier.")
    conversation_id: str | None = Field(default=None, description="External conversation identifier.")
    authorized_scope: dict[str, Any] | None = Field(default=None, description="Reserved explicit authorization scope.")

    @property
    def effective_doctor_id(self) -> int | None:
        return self.actor_doctor_id if self.actor_role == "doctor" else self.target_doctor_id

    @property
    def effective_patient_id(self) -> int | None:
        return self.actor_patient_id if self.actor_role == "patient" else self.target_patient_id

    def allows_doctor_aggregate(self) -> bool:
        scope = self.authorized_scope or {}
        return bool(scope.get("allow_doctor_aggregate"))
