from __future__ import annotations

from models import ReflectionResult, ReviewCard


class ReflectionService:
    def reflect_on_output_placeholder(self) -> ReflectionResult:
        return ReflectionResult(summary_text="pending")

    def reflect_on_output(self, review_card: ReviewCard) -> ReflectionResult:
        missing_fields: list[str] = []
        consistency_notes: list[str] = []
        manual_confirmation_reasons: list[str] = []

        if review_card.plan_summary.session_count == 0:
            missing_fields.append("plan_summary.sessions")
        if review_card.execution_summary.log_count == 0:
            missing_fields.append("execution_summary.logs")
        if review_card.outcome_change.report_count == 0:
            missing_fields.append("outcome_change.reports")
        if review_card.deviation_metrics.risk_level == "high" and not review_card.deviation_metrics.driver_flags:
            consistency_notes.append("高风险标签缺少明确驱动因子，需要人工复核。")
        if review_card.outcome_change.trend_label == "declining" and review_card.deviation_metrics.risk_level == "low":
            consistency_notes.append("结果下降但总体风险偏低，建议人工确认是否低估风险。")
        if review_card.execution_summary.log_count == 0 and review_card.outcome_change.report_count == 0:
            consistency_notes.append("缺少执行日志和结果报告，证据链不完整。")

        evidence_sufficient = not missing_fields or (
            review_card.plan_summary.session_count > 0
            and (review_card.execution_summary.log_count > 0 or review_card.outcome_change.report_count > 0)
        )
        recommend_manual_confirmation = (
            not evidence_sufficient
            or bool(consistency_notes)
            or review_card.deviation_metrics.risk_level == "high"
        )

        if not evidence_sufficient:
            manual_confirmation_reasons.append("关键证据缺失")
        if review_card.deviation_metrics.risk_level == "high":
            manual_confirmation_reasons.append("高风险患者需要人工复核")
        manual_confirmation_reasons.extend(consistency_notes)

        summary_text = "证据充分。" if evidence_sufficient and not recommend_manual_confirmation else "建议人工确认。"
        return ReflectionResult(
            evidence_sufficient=evidence_sufficient,
            missing_fields=missing_fields,
            consistency_notes=consistency_notes,
            recommend_manual_confirmation=recommend_manual_confirmation,
            manual_confirmation_reasons=manual_confirmation_reasons,
            summary_text=summary_text,
        )
