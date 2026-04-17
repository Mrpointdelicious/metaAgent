from __future__ import annotations

from typing import Any

from models import SessionIdentityContext


class MissingIdentityContextError(ValueError):
    pass


def build_session_identity_context(
    *,
    doctor_id: int | None = None,
    patient_id: int | None = None,
    tenant_id: str | None = None,
    org_id: str | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
    authorized_scope: dict[str, Any] | None = None,
) -> SessionIdentityContext:
    if doctor_id is None and patient_id is None:
        raise MissingIdentityContextError("missing_identity_context")
    if doctor_id is not None:
        return SessionIdentityContext(
            actor_role="doctor",
            actor_doctor_id=int(doctor_id),
            target_doctor_id=int(doctor_id),
            target_patient_id=int(patient_id) if patient_id is not None else None,
            tenant_id=tenant_id,
            org_id=org_id,
            session_id=session_id,
            conversation_id=conversation_id,
            authorized_scope=authorized_scope,
        )
    return SessionIdentityContext(
        actor_role="patient",
        actor_patient_id=int(patient_id),  # type: ignore[arg-type]
        target_patient_id=int(patient_id),  # type: ignore[arg-type]
        tenant_id=tenant_id,
        org_id=org_id,
        session_id=session_id,
        conversation_id=conversation_id,
        authorized_scope=authorized_scope,
    )
