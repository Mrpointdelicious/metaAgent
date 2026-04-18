from __future__ import annotations

import uuid
from typing import Any

from agent.schemas import OrchestrationTaskType, OrchestratorRequest
from models import SessionIdentityContext
from server.session_context import build_session_identity_context


def normalize_question_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_service_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def ensure_session_ids(payload: dict[str, Any]) -> tuple[str, str]:
    """Normalize service-level session identifiers in one place.

    The request factory writes these IDs into SessionIdentityContext. The SDK
    runtime later uses session_id as the raw-history key; conversation_id is a
    business tracing ID and does not merge session histories.
    """

    session_id = _normalize_service_id(payload.get("session_id")) or f"sess_{uuid.uuid4().hex}"
    conversation_id = _normalize_service_id(payload.get("conversation_id")) or f"conv_{uuid.uuid4().hex}"
    payload["session_id"] = session_id
    payload["conversation_id"] = conversation_id
    return session_id, conversation_id


def build_orchestrator_request(
    *,
    raw_text: str | None = None,
    question: str | None = None,
    query: str | None = None,
    doctor_id: int | None = None,
    patient_id: int | None = None,
    identity_context: SessionIdentityContext | None = None,
    task_type: str | None = None,
    plan_id: int | None = None,
    days: int | None = None,
    top_k: int = 10,
    use_agent_sdk: bool | None = None,
    llm_provider: Any | None = None,
    llm_model: str | None = None,
    llm_base_url: str | None = None,
    need_outcome: bool | None = None,
    need_gait_evidence: bool | None = None,
    response_style: str | None = None,
    context: dict[str, Any] | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
) -> OrchestratorRequest:
    """Build the only formal request shape used by server and demo adapters.

    Priority for identity-sensitive fields is:
    identity_context -> explicit request fields -> text target hints -> loose context.
    Demo defaults are intentionally not part of this factory.
    """

    if identity_context is None:
        session_id = _normalize_service_id(session_id)
        conversation_id = _normalize_service_id(conversation_id)
        if session_id is None or conversation_id is None:
            generated_session_id, generated_conversation_id = ensure_session_ids({})
            session_id = session_id or generated_session_id
            conversation_id = conversation_id or generated_conversation_id
        identity_context = build_session_identity_context(
            doctor_id=doctor_id,
            patient_id=patient_id,
            session_id=session_id,
            conversation_id=conversation_id,
        )

    effective_doctor_id = doctor_id
    effective_patient_id = patient_id
    if identity_context.actor_role == "doctor":
        effective_doctor_id = identity_context.actor_doctor_id
        effective_patient_id = patient_id if patient_id is not None else identity_context.target_patient_id
    else:
        effective_doctor_id = None
        effective_patient_id = identity_context.actor_patient_id

    return OrchestratorRequest(
        task_type=task_type or OrchestrationTaskType.UNKNOWN.value,
        doctor_id=effective_doctor_id,
        patient_id=effective_patient_id,
        plan_id=plan_id,
        days=days,
        top_k=top_k,
        raw_text=normalize_question_text(raw_text) or normalize_question_text(question) or normalize_question_text(query),
        use_agent_sdk=use_agent_sdk,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_base_url=llm_base_url,
        need_outcome=need_outcome,
        need_gait_evidence=need_gait_evidence,
        response_style=response_style,
        identity_context=identity_context,
        context=context or {},
    )


def build_orchestrator_request_from_payload(payload: dict[str, Any]) -> OrchestratorRequest:
    payload = dict(payload)
    session_id, conversation_id = ensure_session_ids(payload)
    doctor_id = payload.get("doctor_id", payload.get("therapist_id"))
    patient_id = payload.get("patient_id")
    identity_context = build_session_identity_context(
        doctor_id=doctor_id,
        patient_id=patient_id,
        tenant_id=payload.get("tenant_id"),
        org_id=payload.get("org_id"),
        session_id=session_id,
        conversation_id=conversation_id,
        authorized_scope=payload.get("authorized_scope"),
    )
    return build_orchestrator_request(
        task_type=payload.get("task_type"),
        doctor_id=doctor_id,
        patient_id=patient_id,
        identity_context=identity_context,
        plan_id=payload.get("plan_id"),
        days=payload.get("days"),
        top_k=payload.get("top_k", 10),
        raw_text=payload.get("raw_text"),
        question=payload.get("question"),
        query=payload.get("query"),
        use_agent_sdk=payload.get("use_agent_sdk"),
        llm_provider=payload.get("llm_provider"),
        llm_model=payload.get("llm_model"),
        llm_base_url=payload.get("llm_base_url"),
        need_outcome=payload.get("need_outcome"),
        need_gait_evidence=payload.get("need_gait_evidence"),
        response_style=payload.get("response_style"),
        context=payload.get("context") or {},
    )
