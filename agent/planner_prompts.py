from __future__ import annotations

import json
from typing import Any

from .schemas import OrchestratorRequest, RoutedDecision


def build_planner_messages(
    *,
    request: OrchestratorRequest,
    routed_decision: RoutedDecision,
    tool_catalog: list[dict[str, Any]],
    max_steps: int,
) -> list[dict[str, str]]:
    payload = {
        "raw_question": request.raw_text or "",
        "routed_decision": {
            "final_intent": routed_decision.final_intent,
            "final_subtype": routed_decision.final_subtype,
            "final_scope": routed_decision.final_scope,
            "doctor_id_source": routed_decision.doctor_id_source,
            "confidence": routed_decision.confidence,
            "rationale": routed_decision.rationale,
        },
        "request_slots": {
            "therapist_id": request.therapist_id,
            "patient_id": request.patient_id,
            "plan_id": request.plan_id,
            "days": request.days,
            "top_k": request.top_k,
            "analytics_time_slots": request.analytics_time_slots.model_dump(mode="json") if request.analytics_time_slots else None,
        },
        "session_context": request.context or {},
        "tool_catalog": tool_catalog,
        "constraints": [
            "Return only JSON matching the LLMPlannedQuery schema.",
            "Use only tool_name values present in tool_catalog.",
            "Never generate SQL and never request direct database or repository access.",
            "Each step must include step_id, tool_name, arguments, and rationale.",
            f"Use no more than {max_steps} steps.",
            "Arguments must match the tool input schema, except safe step references are allowed as *_ref values.",
            "Allowed set_diff reference arguments: base_set_ref and subtract_set_ref, pointing to prior step_id values.",
            "Allowed patient fan-out reference arguments: patient_set_ref or patient_ids_ref, pointing to a prior patient-set step.",
            "For doctor_aggregate scope, do not add doctor_id or therapist_id filters unless the tool schema explicitly requires them.",
            "For single_doctor scope, include the resolved doctor_id in doctor-scoped primitive tools when available.",
        ],
    }

    return [
        {
            "role": "system",
            "content": (
                "You are a constrained planner for a rehab analytics application. "
                "You only produce structured QueryPlan JSON. You do not execute tools, "
                "do not call repositories, and do not generate SQL. Choose from the provided "
                "primitive tool catalog only."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, indent=2),
        },
    ]
