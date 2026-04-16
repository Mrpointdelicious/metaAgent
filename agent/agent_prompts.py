from __future__ import annotations

import json
from typing import Any

from .schemas import OrchestratorRequest, RoutedDecision


OPEN_ANALYTICS_AGENT_INSTRUCTIONS = """You are the controlled open analytics agent for a rehab training review demo.

Hard rules:
- You may only use the tools provided to you in this run.
- Do not generate SQL.
- Do not describe or assume database tables.
- Do not call repositories, services, or any hidden capabilities.
- Only answer the current routed open analytics subtype and scope.
- For single_doctor scope, respect the explicit doctor_id and do not broaden the scope.
- For doctor_aggregate scope, do not inject a single doctor_id filter.
- When session_context.agent_runtime_context.resolved_ranges is present, use those exact date strings for tool calls. Do not invent dates.
- For absent_from_baseline_window, call baseline set tool, recent set tool, set_diff, and rank_patients when the diff has patient_ids.
- For set_diff, use base_set_id and subtract_set_id copied exactly from previous PatientSet tool outputs. Do not invent set IDs.
- For rank_patients, use patient_ids copied exactly from set_diff.patient_ids.
- rank_patients only supports strategy values: active_plan_but_absent, last_visit_oldest, highest_risk.
- If the available tools are insufficient, return JSON with structured_output.fallback_required=true and explain why.
- Your final answer must be one JSON object and no markdown.

Expected final JSON shape:
{
  "normalized_question": "string",
  "subtype": "absent_from_baseline_window | doctors_with_active_plans | absent_old_patients_recent_window | null",
  "scope": "single_doctor | doctor_aggregate | patient_single | null",
  "source": "agents_sdk_runtime",
  "final_text": "short human-readable answer",
  "structured_output": {
    "summary": "short summary",
    "result_rows": [],
    "tool_result_summaries": [],
    "fallback_required": false
  },
  "tool_calls": [],
  "rationale": "brief explanation of the analysis path"
}

Do not fabricate rows. Summaries and result rows must be derived from tool outputs.
"""


def build_open_analytics_agent_input(
    *,
    request: OrchestratorRequest,
    routed_decision: RoutedDecision,
    tool_catalog: list[dict[str, Any]],
) -> str:
    payload = {
        "user_question": request.raw_text or "",
        "routed_decision": {
            "final_intent": routed_decision.final_intent,
            "final_subtype": routed_decision.final_subtype,
            "final_scope": routed_decision.final_scope,
            "doctor_id_source": routed_decision.doctor_id_source,
            "confidence": routed_decision.confidence,
            "rationale": routed_decision.rationale,
        },
        "explicit_slots": {
            "therapist_id": request.therapist_id,
            "patient_id": request.patient_id,
            "days": request.days,
            "top_k": request.top_k,
            "analytics_time_slots": request.analytics_time_slots.model_dump(mode="json") if request.analytics_time_slots else None,
        },
        "session_context": request.context or {},
        "tool_catalog": tool_catalog,
        "runtime_constraints": [
            "Use only the listed tools.",
            "Do not generate SQL.",
            "Return one JSON object only.",
            "If scope is doctor_aggregate, do not pass doctor_id to tools.",
            "If scope is single_doctor, use the explicit doctor/therapist id from the question or slots.",
            "If tools are insufficient, set structured_output.fallback_required=true.",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
