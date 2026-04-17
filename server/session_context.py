from __future__ import annotations

from typing import Any

from agent.schemas import OrchestrationTaskType, OrchestratorRequest
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


def build_orchestrator_request_from_payload(payload: dict[str, Any]) -> OrchestratorRequest:
    doctor_id = payload.get("doctor_id", payload.get("therapist_id"))
    patient_id = payload.get("patient_id")
    identity_context = build_session_identity_context(
        doctor_id=doctor_id,
        patient_id=patient_id,
        tenant_id=payload.get("tenant_id"),
        org_id=payload.get("org_id"),
        session_id=payload.get("session_id"),
        conversation_id=payload.get("conversation_id"),
        authorized_scope=payload.get("authorized_scope"),
    )
    return OrchestratorRequest(
        task_type=payload.get("task_type") or OrchestrationTaskType.UNKNOWN.value,
        doctor_id=doctor_id,
        patient_id=patient_id,
        plan_id=payload.get("plan_id"),
        days=payload.get("days"),
        top_k=payload.get("top_k", 10),
        raw_text=payload.get("raw_text") or payload.get("question"),
        use_agent_sdk=payload.get("use_agent_sdk"),
        llm_provider=payload.get("llm_provider"),
        llm_model=payload.get("llm_model"),
        llm_base_url=payload.get("llm_base_url"),
        need_outcome=payload.get("need_outcome"),
        need_gait_evidence=payload.get("need_gait_evidence"),
        response_style=payload.get("response_style"),
        identity_context=identity_context,
        context=payload.get("context") or {},
    )
