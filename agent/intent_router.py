from __future__ import annotations

import logging
import re

from .schemas import (
    AnalyticsScope,
    DoctorIdSource,
    IntentDecision,
    OpenAnalyticsSubtype,
    OrchestrationTaskType,
    OrchestratorRequest,
    normalize_task_type,
)


logger = logging.getLogger(__name__)

WEEKLY_KEYWORDS = ("周报", "weekly", "风险摘要", "summary")
SCREEN_KEYWORDS = ("筛选", "高风险", "优先复核", "risk", "screen")
REVIEW_KEYWORDS = ("复核", "review card", "review", "计划", "患者", "病人", "patient", "plan")
OPEN_ANALYTICS_KEYWORDS = (
    "哪些",
    "多少",
    "以前来过",
    "之前来过",
    "曾经来过",
    "没有来",
    "没来",
    "未到训",
    "未到",
    "比较",
    "统计",
    "名单",
    "差集",
    "训练计划",
    "定计划",
    "活跃计划",
)
ABSENT_PRIOR_VISIT_KEYWORDS = ("以前来过", "之前来过", "曾经来过", "来过")
ABSENT_MISSING_KEYWORDS = ("最近没来", "没有来", "没来", "未到训", "没到", "未到")
PLAN_ACTIVITY_KEYWORDS = ("训练计划", "定患者训练计划", "定计划", "活跃计划", "计划")
DOCTOR_AGGREGATE_KEYWORDS = ("哪些医生", "哪些治疗师", "各医生", "各治疗师", "医生列表", "治疗师列表")
BASELINE_KEYWORDS = ("baseline", "基线", "前一阶段", "前一段时间", "前80-30天", "前80天到前30天")


# Real UTF-8 aliases are added here because older demo-era constants in this
# file may contain mojibake copies kept for backward compatibility with tests.
WEEKLY_KEYWORDS = WEEKLY_KEYWORDS + ("周报", "风险摘要", "摘要")
SCREEN_KEYWORDS = SCREEN_KEYWORDS + ("筛选", "高风险", "风险筛选", "优先复核")
REVIEW_KEYWORDS = REVIEW_KEYWORDS + ("复核", "计划", "患者", "病人")
OPEN_ANALYTICS_KEYWORDS = OPEN_ANALYTICS_KEYWORDS + (
    "哪些",
    "多少",
    "以前来过",
    "之前来过",
    "曾经来过",
    "没有来",
    "没来",
    "未到训",
    "未到",
    "比较",
    "统计",
    "名单",
    "差集",
    "训练计划",
    "定计划",
    "活跃计划",
)
ABSENT_PRIOR_VISIT_KEYWORDS = ABSENT_PRIOR_VISIT_KEYWORDS + ("以前来过", "之前来过", "曾经来过", "来过")
ABSENT_MISSING_KEYWORDS = ABSENT_MISSING_KEYWORDS + ("最近没来", "没有来", "没来", "未到训", "没到", "未到")
PLAN_ACTIVITY_KEYWORDS = PLAN_ACTIVITY_KEYWORDS + ("训练计划", "定患者训练计划", "定计划", "活跃计划", "计划")
DOCTOR_AGGREGATE_KEYWORDS = DOCTOR_AGGREGATE_KEYWORDS + ("哪些医生", "哪些治疗师", "各医生", "各治疗师", "医生列表", "治疗师列表")
BASELINE_KEYWORDS = BASELINE_KEYWORDS + ("基线", "前一阶段", "前一段时间", "前80-30天", "前80天到前30天")
LOOKUP_SIGNAL_KEYWORDS = ("名字", "姓名", "叫什么", "是谁", "who is", "name")


class IntentRouter:
    def route(self, request: OrchestratorRequest) -> IntentDecision:
        normalized = normalize_task_type(request.task_type)
        identity = request.identity_context
        if identity and identity.actor_role == "patient" and normalized in {
            OrchestrationTaskType.SCREEN_RISK,
            OrchestrationTaskType.WEEKLY_REPORT,
        }:
            return IntentDecision(
                intent="open_analytics_query",
                confidence=0.99,
                rationale="Patient identity cannot be routed to group doctor workflows.",
                analytics_subtype=None,
                analysis_scope=None,
                doctor_id_source="none",
            )
        if normalized == OrchestrationTaskType.REVIEW_PATIENT:
            return IntentDecision(intent="single_patient_review", confidence=0.99, rationale="Task type explicitly requests patient review.")
        if normalized == OrchestrationTaskType.SCREEN_RISK:
            return IntentDecision(intent="risk_screening", confidence=0.99, rationale="Task type explicitly requests risk screening.")
        if normalized == OrchestrationTaskType.WEEKLY_REPORT:
            return IntentDecision(intent="weekly_report", confidence=0.99, rationale="Task type explicitly requests weekly report.")
        if normalized == OrchestrationTaskType.LOOKUP_QUERY:
            lookup = self._detect_lookup_query(request, request.raw_text or "")
            return lookup or IntentDecision(
                intent="lookup_query",
                confidence=0.55,
                rationale="Task type explicitly requests lookup, but no entity ID was detected.",
                lookup_subtype="lookup_user_name",
                lookup_entity_type="unknown",
                lookup_user_id=None,
            )
        if normalized == OrchestrationTaskType.OPEN_ANALYTICS_QUERY:
            return self._build_open_analytics_decision(
                request,
                base_confidence=0.99,
                fallback_rationale="Task type explicitly requests open analytics.",
            )

        raw_text = (request.raw_text or "").strip()
        lowered = raw_text.lower()
        if not raw_text:
            return self._build_open_analytics_decision(
                request,
                base_confidence=0.2,
                fallback_rationale="No fixed task matched, falling back to open analytics.",
            )

        lookup_decision = self._detect_lookup_query(request, raw_text)
        if lookup_decision is not None:
            return lookup_decision

        if any(keyword in raw_text or keyword in lowered for keyword in WEEKLY_KEYWORDS):
            return IntentDecision(intent="weekly_report", confidence=0.9, rationale="Matched weekly report keywords.")
        if any(keyword in raw_text or keyword in lowered for keyword in SCREEN_KEYWORDS):
            return IntentDecision(intent="risk_screening", confidence=0.88, rationale="Matched risk screening keywords.")
        if self._is_identifier_only_query(raw_text):
            return self._build_open_analytics_decision(
                request,
                base_confidence=0.35,
                fallback_rationale="Identifier-only request is ambiguous; leaving route open for LLM refinement or clarification.",
            )
        if self._has_patient_or_plan_identifier(raw_text) or any(
            keyword in raw_text or keyword in lowered for keyword in REVIEW_KEYWORDS
        ):
            return IntentDecision(intent="single_patient_review", confidence=0.82, rationale="Matched patient review keywords or IDs.")

        has_doctor_scope = self._has_doctor_scope(raw_text)
        has_time_window = self._has_time_window(raw_text)
        has_analytics_signal = any(keyword in raw_text or keyword in lowered for keyword in OPEN_ANALYTICS_KEYWORDS)
        if has_analytics_signal and (has_doctor_scope or has_time_window):
            return self._build_open_analytics_decision(
                request,
                base_confidence=0.86,
                fallback_rationale="Matched open analytics keywords with scope or time window.",
            )
        if has_doctor_scope and has_time_window:
            return self._build_open_analytics_decision(
                request,
                base_confidence=0.72,
                fallback_rationale="Matched doctor scope plus time window, likely open analytics.",
            )

        return self._build_open_analytics_decision(
            request,
            base_confidence=0.45,
            fallback_rationale="No fixed workflow matched, falling back to open analytics.",
        )

    def _detect_lookup_query(self, request: OrchestratorRequest, text: str) -> IntentDecision | None:
        del request
        if not text:
            return None
        lowered = text.lower()
        has_lookup_signal = any(keyword in text or keyword in lowered for keyword in LOOKUP_SIGNAL_KEYWORDS)
        if not has_lookup_signal:
            return None

        doctor_id = self._extract_entity_id(text, ("医生", "治疗师", "康复师", "doctor", "therapist"))
        patient_id = self._extract_entity_id(text, ("患者", "病人", "patient"))
        if doctor_id is not None:
            return IntentDecision(
                intent="lookup_query",
                confidence=0.94,
                rationale="Matched doctor name/entity lookup pattern.",
                lookup_subtype="lookup_user_name",
                lookup_entity_type="doctor",
                lookup_user_id=doctor_id,
            )
        if patient_id is not None:
            return IntentDecision(
                intent="lookup_query",
                confidence=0.94,
                rationale="Matched patient name/entity lookup pattern.",
                lookup_subtype="lookup_user_name",
                lookup_entity_type="patient",
                lookup_user_id=patient_id,
            )

        bare_id = self._extract_bare_lookup_id(text)
        if bare_id is not None:
            return IntentDecision(
                intent="lookup_query",
                confidence=0.72,
                rationale="Matched bare user ID lookup; entity type is ambiguous.",
                lookup_subtype="lookup_user_name",
                lookup_entity_type="unknown",
                lookup_user_id=bare_id,
            )
        return None

    def _is_identifier_only_query(self, text: str) -> bool:
        compact = re.sub(r"[\s,，。?？:：-]+", "", text).lower()
        if not compact:
            return False
        if re.fullmatch(r"(?:医生|治疗师|康复师|doctor|therapist)(?:id)?\d+", compact, flags=re.IGNORECASE):
            return True
        if re.fullmatch(r"(?:患者|病人|patient)(?:id)?\d+", compact, flags=re.IGNORECASE):
            return True
        return bool(re.fullmatch(r"\d+", compact))

    def _extract_entity_id(self, text: str, labels: tuple[str, ...]) -> int | None:
        for label in labels:
            match = re.search(rf"{re.escape(label)}\s*(?:id)?\s*[:：]?\s*(\d+)", text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    def _extract_bare_lookup_id(self, text: str) -> int | None:
        for pattern in (
            r"(?:查询|查一下|看一下|看看)?\s*(\d+)\s*(?:是谁|叫什么|的名字|的姓名)",
            r"(?:who\s+is|name\s+of)\s*(\d+)",
        ):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    def _build_open_analytics_decision(
        self,
        request: OrchestratorRequest,
        *,
        base_confidence: float,
        fallback_rationale: str,
    ) -> IntentDecision:
        raw_text = (request.raw_text or "").strip()
        subtype, rationale = self._detect_open_analytics_subtype(raw_text)
        scope = self._scope_for_subtype(subtype)
        decision = IntentDecision(
            intent="open_analytics_query",
            confidence=base_confidence,
            rationale=rationale or fallback_rationale,
            analytics_subtype=subtype,
            analysis_scope=scope,
            doctor_id_source=self._doctor_id_source(request, raw_text, scope),
        )
        logger.info(
            "open analytics routed subtype=%s scope=%s confidence=%.2f question=%r",
            decision.analytics_subtype,
            decision.analysis_scope,
            decision.confidence,
            raw_text,
        )
        return decision

    def _detect_open_analytics_subtype(self, text: str) -> tuple[OpenAnalyticsSubtype | None, str | None]:
        lowered = text.lower()
        has_absent_signal = self._has_any(text, lowered, ABSENT_MISSING_KEYWORDS)
        has_prior_visit_signal = self._has_any(text, lowered, ABSENT_PRIOR_VISIT_KEYWORDS)
        has_plan_signal = self._has_any(text, lowered, PLAN_ACTIVITY_KEYWORDS)
        has_dual_window_signal = self._has_dual_window_signal(text)
        has_time_window = self._has_time_window(text)

        if self._has_doctor_aggregate_subject(text) and has_plan_signal:
            return "doctors_with_active_plans", "Matched doctor aggregate subject plus active plan keywords."
        if has_dual_window_signal and has_absent_signal and (has_prior_visit_signal or "患者" in text or "patient" in lowered):
            return "absent_from_baseline_window", "Matched dual-window baseline wording plus absence signals."
        if has_prior_visit_signal and has_absent_signal and (has_time_window or self._has_doctor_scope(text)):
            return "absent_old_patients_recent_window", "Matched prior-visit wording plus recent absence signals."
        return None, None

    def _has_patient_or_plan_identifier(self, text: str) -> bool:
        return bool(
            re.search(r"(患者|病人|patient)\s*(?:id)?\s*[:：]?\s*\d+", text, flags=re.IGNORECASE)
            or re.search(r"(计划|plan)\s*(?:id)?\s*[:：]?\s*\d+", text, flags=re.IGNORECASE)
        )

    def _has_doctor_scope(self, text: str) -> bool:
        lowered = text.lower()
        return any(keyword in text or keyword in lowered for keyword in ("医生", "治疗师", "康复师", "doctor", "therapist"))

    def _has_doctor_aggregate_subject(self, text: str) -> bool:
        lowered = text.lower()
        if any(keyword in text or keyword in lowered for keyword in DOCTOR_AGGREGATE_KEYWORDS):
            return True
        return bool(re.search(r"(which|what)\s+doctors?", lowered))

    def _has_time_window(self, text: str) -> bool:
        lowered = text.lower()
        if any(keyword in text or keyword in lowered for keyword in ("本周", "本月", "最近", "近", "这", "last week", "last month")):
            return True
        return bool(re.search(r"(\d+)\s*天", text) or re.search(r"last\s*(\d+)\s*days?", lowered))

    def _has_dual_window_signal(self, text: str) -> bool:
        lowered = text.lower()
        if any(keyword in text or keyword in lowered for keyword in BASELINE_KEYWORDS):
            return True
        patterns = (
            r"前\s*\d+\s*[-到至]\s*\d+\s*天",
            r"前\s*\d+\s*天\s*(?:到|至|-)\s*前\s*\d+\s*天",
            r"过去\s*\d+\s*天.*(?:排除|除去|除掉|去掉).*\d+\s*天",
        )
        return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)

    def _has_any(self, text: str, lowered: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword in text or keyword in lowered for keyword in keywords)

    def _scope_for_subtype(self, subtype: OpenAnalyticsSubtype | None) -> AnalyticsScope | None:
        if subtype == "doctors_with_active_plans":
            return "doctor_aggregate"
        if subtype in {"absent_old_patients_recent_window", "absent_from_baseline_window"}:
            return "single_doctor"
        return None

    def _doctor_id_source(
        self,
        request: OrchestratorRequest,
        text: str,
        scope: AnalyticsScope | None,
    ) -> DoctorIdSource | None:
        if scope == "doctor_aggregate":
            return "none"
        if request.identity_context is not None:
            if request.identity_context.actor_role == "doctor" and scope == "single_doctor":
                return "session"
            if request.identity_context.actor_role == "patient":
                return "none"
        if self._extract_doctor_id(text) is not None:
            return "explicit"
        if scope == "single_doctor" and (request.therapist_id is not None or (request.context or {}).get("therapist_id") is not None):
            return "session"
        if scope == "single_doctor":
            return "session"
        return None

    def _extract_doctor_id(self, text: str) -> int | None:
        match = re.search(
            r"(?:医生|治疗师|康复师|doctor|therapist)\s*(?:id)?\s*[:：]?\s*(\d+)",
            text,
            flags=re.IGNORECASE,
        )
        return int(match.group(1)) if match else None

    def _has_patient_or_plan_identifier(self, text: str) -> bool:
        return bool(
            re.search(r"(?:患者|病人|patient)\s*(?:id)?\s*[:：]?\s*\d+", text, flags=re.IGNORECASE)
            or re.search(r"(?:计划|plan)\s*(?:id)?\s*[:：]?\s*\d+", text, flags=re.IGNORECASE)
        )

    def _has_doctor_scope(self, text: str) -> bool:
        lowered = text.lower()
        return any(
            keyword in text or keyword in lowered
            for keyword in ("医生", "治疗师", "康复师", "doctor", "therapist", "鍖荤敓", "娌荤枟甯?", "搴峰甯?")
        )

    def _has_time_window(self, text: str) -> bool:
        lowered = text.lower()
        if any(
            keyword in text or keyword in lowered
            for keyword in ("本周", "本月", "最近", "近", "过去", "last week", "last month", "鏈懆", "鏈湀", "鏈€杩?", "杩?")
        ):
            return True
        return bool(re.search(r"(\d+)\s*天", text) or re.search(r"last\s*(\d+)\s*days?", lowered))

    def _extract_doctor_id(self, text: str) -> int | None:
        return self._extract_entity_id(text, ("医生", "治疗师", "康复师", "doctor", "therapist", "鍖荤敓", "娌荤枟甯?", "搴峰甯?"))
