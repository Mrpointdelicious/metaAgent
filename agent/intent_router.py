from __future__ import annotations

import re

from .schemas import IntentDecision, OrchestrationTaskType, OrchestratorRequest, normalize_task_type


WEEKLY_KEYWORDS = ("周报", "weekly", "风险摘要", "summary")
SCREEN_KEYWORDS = ("筛选", "高风险", "优先复核", "risk", "screen")
REVIEW_KEYWORDS = ("复核", "review card", "review", "计划", "患者", "patient", "plan")
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
)


class IntentRouter:
    def route(self, request: OrchestratorRequest) -> IntentDecision:
        normalized = normalize_task_type(request.task_type)
        if normalized == OrchestrationTaskType.REVIEW_PATIENT:
            return IntentDecision(intent="single_patient_review", confidence=0.99, rationale="命令层已明确指定单患者复核。")
        if normalized == OrchestrationTaskType.SCREEN_RISK:
            return IntentDecision(intent="risk_screening", confidence=0.99, rationale="命令层已明确指定风险筛选。")
        if normalized == OrchestrationTaskType.WEEKLY_REPORT:
            return IntentDecision(intent="weekly_report", confidence=0.99, rationale="命令层已明确指定周报任务。")
        if normalized == OrchestrationTaskType.OPEN_ANALYTICS_QUERY:
            return IntentDecision(intent="open_analytics_query", confidence=0.99, rationale="命令层已明确指定开放式分析。")

        raw_text = (request.raw_text or "").strip()
        lowered = raw_text.lower()
        if not raw_text:
            return IntentDecision(intent="open_analytics_query", confidence=0.2, rationale="未提供固定任务指令，默认交给开放分析路由进一步判定。")

        if any(keyword in raw_text or keyword in lowered for keyword in WEEKLY_KEYWORDS):
            return IntentDecision(intent="weekly_report", confidence=0.9, rationale="命中周报关键词。")
        if any(keyword in raw_text or keyword in lowered for keyword in SCREEN_KEYWORDS):
            return IntentDecision(intent="risk_screening", confidence=0.88, rationale="命中风险筛选关键词。")
        if (
            self._has_patient_or_plan_identifier(raw_text)
            or any(keyword in raw_text or keyword in lowered for keyword in REVIEW_KEYWORDS)
        ):
            return IntentDecision(intent="single_patient_review", confidence=0.82, rationale="命中单患者复核关键词或计划/患者标识。")

        has_doctor_scope = self._has_doctor_scope(raw_text)
        has_time_window = self._has_time_window(raw_text)
        has_analytics_signal = any(keyword in raw_text or keyword in lowered for keyword in OPEN_ANALYTICS_KEYWORDS)
        if has_analytics_signal and (has_doctor_scope or has_time_window):
            return IntentDecision(intent="open_analytics_query", confidence=0.86, rationale="命中集合/比较类分析关键词，并带有医生或时间窗范围。")
        if has_doctor_scope and has_time_window:
            return IntentDecision(intent="open_analytics_query", confidence=0.72, rationale="问题包含医生范围和时间窗，更像开放式分析问句。")

        return IntentDecision(intent="open_analytics_query", confidence=0.45, rationale="未命中固定高频任务，回退到开放式分析支路。")

    def _has_patient_or_plan_identifier(self, text: str) -> bool:
        return bool(
            re.search(r"(患者|病人|patient)\s*(?:id)?\s*[:：]?\s*\d+", text, flags=re.IGNORECASE)
            or re.search(r"(计划|plan)\s*(?:id)?\s*[:：]?\s*\d+", text, flags=re.IGNORECASE)
        )

    def _has_doctor_scope(self, text: str) -> bool:
        lowered = text.lower()
        return any(keyword in text or keyword in lowered for keyword in ("医生", "治疗师", "康复师", "doctor", "therapist"))

    def _has_time_window(self, text: str) -> bool:
        lowered = text.lower()
        if any(keyword in text or keyword in lowered for keyword in ("本周", "本月", "最近", "近", "last week", "last month")):
            return True
        return bool(re.search(r"(\d+)\s*天", text) or re.search(r"last\s*(\d+)\s*days?", lowered))
