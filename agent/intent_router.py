from __future__ import annotations

import logging
import re

from .schemas import (
    AnalyticsScope,
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


class IntentRouter:
    def route(self, request: OrchestratorRequest) -> IntentDecision:
        normalized = normalize_task_type(request.task_type)
        if normalized == OrchestrationTaskType.REVIEW_PATIENT:
            return IntentDecision(intent="single_patient_review", confidence=0.99, rationale="Task type explicitly requests patient review.")
        if normalized == OrchestrationTaskType.SCREEN_RISK:
            return IntentDecision(intent="risk_screening", confidence=0.99, rationale="Task type explicitly requests risk screening.")
        if normalized == OrchestrationTaskType.WEEKLY_REPORT:
            return IntentDecision(intent="weekly_report", confidence=0.99, rationale="Task type explicitly requests weekly report.")
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

        if any(keyword in raw_text or keyword in lowered for keyword in WEEKLY_KEYWORDS):
            return IntentDecision(intent="weekly_report", confidence=0.9, rationale="Matched weekly report keywords.")
        if any(keyword in raw_text or keyword in lowered for keyword in SCREEN_KEYWORDS):
            return IntentDecision(intent="risk_screening", confidence=0.88, rationale="Matched risk screening keywords.")
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
