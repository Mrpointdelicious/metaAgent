from __future__ import annotations

import json
import logging
import re
from typing import Any

from config import ResolvedLLMConfig, Settings, get_settings

from .schemas import (
    AnalyticsScope,
    IntentDecision,
    LLMRouteDecision,
    OpenAnalyticsSubtype,
    OrchestratorRequest,
    RoutedDecision,
)


logger = logging.getLogger(__name__)

SUPPORTED_SUBTYPES: tuple[OpenAnalyticsSubtype, ...] = (
    "absent_old_patients_recent_window",
    "absent_from_baseline_window",
    "doctors_with_active_plans",
)
SUPPORTED_SCOPES: tuple[AnalyticsScope, ...] = (
    "single_doctor",
    "doctor_aggregate",
    "patient_single",
)
FIXED_INTENTS = {"single_patient_review", "risk_screening", "weekly_report", "lookup_query"}


class LLMRouter:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def should_refine(self, request: OrchestratorRequest, rule_decision: IntentDecision) -> bool:
        if rule_decision.intent in FIXED_INTENTS and rule_decision.confidence >= 0.95:
            return False

        text = (request.raw_text or "").strip()
        if rule_decision.intent == "open_analytics_query":
            return (
                rule_decision.confidence < 0.9
                or rule_decision.analytics_subtype is None
                or self._has_dual_window_signal(text)
                or self._has_doctor_aggregate_signal(text)
                or self._is_follow_up(text)
            )

        return rule_decision.confidence < 0.75 or self._is_follow_up(text)

    def refine(
        self,
        request: OrchestratorRequest,
        rule_decision: IntentDecision,
        *,
        llm_config: ResolvedLLMConfig,
        mode: str,
    ) -> LLMRouteDecision:
        if mode != "agents_sdk":
            return self._fallback_decision(rule_decision, "LLM refinement skipped because execution mode does not allow LLM routing.")
        if not llm_config.can_use_agents_sdk:
            return self._fallback_decision(rule_decision, "LLM refinement skipped because provider/model credentials are incomplete.")
        if not llm_config.model:
            return self._fallback_decision(rule_decision, "LLM refinement skipped because no model is configured.")

        try:
            return self._call_llm_router(request, rule_decision, llm_config=llm_config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm router refinement failed: %s", exc)
            return self._fallback_decision(rule_decision, f"LLM refinement failed and fell back to rules: {type(exc).__name__}.")

    def _call_llm_router(
        self,
        request: OrchestratorRequest,
        rule_decision: IntentDecision,
        *,
        llm_config: ResolvedLLMConfig,
    ) -> LLMRouteDecision:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai package is not installed") from exc

        client_kwargs: dict[str, Any] = {"api_key": llm_config.api_key}
        if llm_config.base_url:
            client_kwargs["base_url"] = llm_config.base_url
        client = OpenAI(**client_kwargs)

        messages = [
            {
                "role": "system",
                "content": self._system_prompt(),
            },
            {
                "role": "user",
                "content": json.dumps(
                    self._build_router_payload(request, rule_decision),
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ]
        schema = LLMRouteDecision.model_json_schema()
        try:
            response = client.chat.completions.create(
                model=llm_config.model,
                messages=messages,
                temperature=0,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "LLMRouteDecision",
                        "schema": schema,
                        "strict": True,
                    },
                },
            )
        except Exception:
            response = client.chat.completions.create(
                model=llm_config.model,
                messages=messages,
                temperature=0,
                response_format={"type": "json_object"},
            )

        content = response.choices[0].message.content if response.choices else ""
        if not content:
            raise ValueError("empty llm router response")
        payload = json.loads(content)
        return LLMRouteDecision.model_validate(payload)

    def _system_prompt(self) -> str:
        return (
            "You are a routing classifier for a rehab analytics application. "
            "Return only JSON that matches the provided schema. "
            "You identify intent, open analytics subtype, scope, doctor_id_source, confidence, and rationale. "
            "Never generate SQL. Never call tools. Never execute the request. "
            "Supported intents: single_patient_review, risk_screening, weekly_report, open_analytics_query, lookup_query. "
            "Supported open analytics subtypes: absent_old_patients_recent_window, absent_from_baseline_window, doctors_with_active_plans. "
            "Supported scopes: single_doctor, doctor_aggregate, patient_single. "
            "Lookup queries such as doctor name / patient name / who is this ID should use intent=lookup_query, "
            "lookup_subtype=lookup_user_name, lookup_entity_type doctor/patient/unknown, and lookup_user_id. "
            "Roster queries such as my patients or my doctors should use lookup_subtype=list_my_patients or list_my_doctors. "
            "Scope rules: explicit doctor ID wins; single doctor analytics may inherit session doctor ID; "
            "doctor aggregate questions such as which doctors / each doctor / whole hospital must ignore session doctor as a filter."
        )

    def _build_router_payload(
        self,
        request: OrchestratorRequest,
        rule_decision: IntentDecision,
    ) -> dict[str, Any]:
        return {
            "raw_question": request.raw_text or "",
            "request_slots": {
                "therapist_id": request.therapist_id,
                "doctor_id": request.doctor_id,
                "patient_id": request.patient_id,
                "plan_id": request.plan_id,
                "days": request.days,
                "task_type": request.task_type,
            },
            "identity_context": request.identity_context.model_dump(mode="json") if request.identity_context else None,
            "session_context": request.context or {},
            "rule_decision": rule_decision.model_dump(mode="json"),
            "supported_open_analytics_subtypes": list(SUPPORTED_SUBTYPES),
            "supported_scopes": list(SUPPORTED_SCOPES),
            "scope_rules": [
                "explicit doctor_id wins",
                "single_doctor analytics can inherit session doctor_id",
                "doctor_aggregate analytics must not inherit session doctor_id as a filter",
            ],
        }

    def _fallback_decision(self, rule_decision: IntentDecision, rationale: str) -> LLMRouteDecision:
        return LLMRouteDecision(
            intent=rule_decision.intent,
            analytics_subtype=rule_decision.analytics_subtype,
            scope=rule_decision.analysis_scope,
            doctor_id_source=rule_decision.doctor_id_source,
            lookup_subtype=rule_decision.lookup_subtype,
            lookup_entity_type=rule_decision.lookup_entity_type,
            lookup_user_id=rule_decision.lookup_user_id,
            confidence=rule_decision.confidence,
            rationale=rationale,
        )

    def _has_dual_window_signal(self, text: str) -> bool:
        lowered = text.lower()
        if any(keyword in text or keyword in lowered for keyword in ("baseline", "基线", "前一阶段", "前一段时间")):
            return True
        patterns = (
            r"前\s*\d+\s*[-到至]\s*\d+\s*天",
            r"前\s*\d+\s*天\s*(?:到|至|-)\s*前\s*\d+\s*天",
            r"过去\s*\d+\s*天.*(?:排除|除去|除掉|去掉).*\d+\s*天",
        )
        return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)

    def _has_doctor_aggregate_signal(self, text: str) -> bool:
        lowered = text.lower()
        return any(
            keyword in text or keyword in lowered
            for keyword in ("哪些医生", "哪些治疗师", "各医生", "各治疗师", "全院", "doctor list", "which doctors")
        )

    def _is_follow_up(self, text: str) -> bool:
        lowered = text.lower()
        return any(keyword in text or keyword in lowered for keyword in ("这个", "该", "继续", "刚才", "same", "previous", "this"))


def merge_rule_and_llm(
    rule_decision: IntentDecision,
    llm_decision: LLMRouteDecision | None,
) -> RoutedDecision:
    if llm_decision is None:
        return RoutedDecision(
            rule_decision=rule_decision,
            llm_decision=None,
            final_intent=rule_decision.intent,
            final_subtype=rule_decision.analytics_subtype,
            final_scope=rule_decision.analysis_scope,
            doctor_id_source=rule_decision.doctor_id_source,
            lookup_subtype=rule_decision.lookup_subtype,
            lookup_entity_type=rule_decision.lookup_entity_type,
            lookup_user_id=rule_decision.lookup_user_id,
            confidence=rule_decision.confidence,
            rationale=f"rule_only: {rule_decision.rationale}",
        )

    if rule_decision.intent in FIXED_INTENTS and rule_decision.confidence >= 0.95:
        return RoutedDecision(
            rule_decision=rule_decision,
            llm_decision=llm_decision,
            final_intent=rule_decision.intent,
            final_subtype=rule_decision.analytics_subtype,
            final_scope=rule_decision.analysis_scope,
            doctor_id_source=llm_decision.doctor_id_source or rule_decision.doctor_id_source,
            lookup_subtype=llm_decision.lookup_subtype or rule_decision.lookup_subtype,
            lookup_entity_type=llm_decision.lookup_entity_type or rule_decision.lookup_entity_type,
            lookup_user_id=llm_decision.lookup_user_id or rule_decision.lookup_user_id,
            confidence=rule_decision.confidence,
            rationale=f"kept high-confidence fixed rule decision; llm={llm_decision.rationale}",
        )

    if rule_decision.intent == "open_analytics_query":
        final_intent = llm_decision.intent if llm_decision.confidence >= 0.55 else rule_decision.intent
        final_subtype = llm_decision.analytics_subtype or rule_decision.analytics_subtype
        final_scope = llm_decision.scope or rule_decision.analysis_scope
        confidence = max(rule_decision.confidence, llm_decision.confidence)
        return RoutedDecision(
            rule_decision=rule_decision,
            llm_decision=llm_decision,
            final_intent=final_intent,
            final_subtype=final_subtype,
            final_scope=final_scope,
            doctor_id_source=llm_decision.doctor_id_source or rule_decision.doctor_id_source,
            lookup_subtype=llm_decision.lookup_subtype or rule_decision.lookup_subtype,
            lookup_entity_type=llm_decision.lookup_entity_type or rule_decision.lookup_entity_type,
            lookup_user_id=llm_decision.lookup_user_id or rule_decision.lookup_user_id,
            confidence=confidence,
            rationale=f"open analytics merged rule and llm; rule={rule_decision.rationale}; llm={llm_decision.rationale}",
        )

    if llm_decision.confidence > rule_decision.confidence + 0.15:
        return RoutedDecision(
            rule_decision=rule_decision,
            llm_decision=llm_decision,
            final_intent=llm_decision.intent,
            final_subtype=llm_decision.analytics_subtype,
            final_scope=llm_decision.scope,
            doctor_id_source=llm_decision.doctor_id_source or rule_decision.doctor_id_source,
            lookup_subtype=llm_decision.lookup_subtype or rule_decision.lookup_subtype,
            lookup_entity_type=llm_decision.lookup_entity_type or rule_decision.lookup_entity_type,
            lookup_user_id=llm_decision.lookup_user_id or rule_decision.lookup_user_id,
            confidence=llm_decision.confidence,
            rationale=f"llm override because confidence is materially higher; rule={rule_decision.rationale}; llm={llm_decision.rationale}",
        )

    return RoutedDecision(
        rule_decision=rule_decision,
        llm_decision=llm_decision,
        final_intent=rule_decision.intent,
        final_subtype=rule_decision.analytics_subtype or llm_decision.analytics_subtype,
        final_scope=rule_decision.analysis_scope or llm_decision.scope,
        doctor_id_source=llm_decision.doctor_id_source or rule_decision.doctor_id_source,
        lookup_subtype=rule_decision.lookup_subtype or llm_decision.lookup_subtype,
        lookup_entity_type=rule_decision.lookup_entity_type or llm_decision.lookup_entity_type,
        lookup_user_id=rule_decision.lookup_user_id or llm_decision.lookup_user_id,
        confidence=max(rule_decision.confidence, llm_decision.confidence),
        rationale=f"kept rule intent with llm enrichment; rule={rule_decision.rationale}; llm={llm_decision.rationale}",
    )
