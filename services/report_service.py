from __future__ import annotations

from datetime import datetime

from models import PatientRiskSummary, ReviewCard, WeeklyRiskReport
from repositories import RehabRepository

from .deviation_service import DeviationService
from .execution_service import ExecutionService
from .gait_service import GaitService
from .outcome_service import OutcomeService
from .plan_service import PlanService
from .reflection_service import ReflectionService
from .shared import build_time_range


class ReportService:
    def __init__(
        self,
        repository: RehabRepository,
        plan_service: PlanService,
        execution_service: ExecutionService,
        outcome_service: OutcomeService,
        gait_service: GaitService,
        deviation_service: DeviationService,
        reflection_service: ReflectionService,
    ):
        self.repository = repository
        self.plan_service = plan_service
        self.execution_service = execution_service
        self.outcome_service = outcome_service
        self.gait_service = gait_service
        self.deviation_service = deviation_service
        self.reflection_service = reflection_service

    def generate_review_card(
        self,
        *,
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> ReviewCard:
        plan_summary = self.plan_service.get_plan_summary(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
            start=start,
            end=end,
        )
        execution_summary = self.execution_service.get_execution_logs(
            patient_id=patient_id or plan_summary.patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id or plan_summary.therapist_id,
            plan_summary=plan_summary,
        )
        outcome_change = self.outcome_service.get_outcome_change(
            patient_id=patient_id or plan_summary.patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id or plan_summary.therapist_id,
            plan_summary=plan_summary,
        )
        gait_explanation = self.gait_service.get_gait_explanation(
            patient_id=patient_id or plan_summary.patient_id,
            days=days,
            start=plan_summary.time_range.start,
            end=plan_summary.time_range.end,
        )
        deviation_metrics = self.deviation_service.calc_deviation_metrics(
            plan_summary=plan_summary,
            execution_summary=execution_summary,
            outcome_change=outcome_change,
        )

        review_focus: list[str] = []
        initial_interventions: list[str] = []
        if "low_attendance" in deviation_metrics.driver_flags:
            review_focus.append("确认患者近期是否按预约到训")
            initial_interventions.append("联系治疗师核对排班与患者到院情况")
        if "low_completion" in deviation_metrics.driver_flags:
            review_focus.append("确认训练是否中途终止或记录不完整")
            initial_interventions.append("复核设备结束记录与训练结束原因")
        if "low_dose" in deviation_metrics.driver_flags:
            review_focus.append("确认计划剂量与实际执行剂量差异")
            initial_interventions.append("检查计划参数是否过高或患者耐受不足")
        if "outcome_declining" in deviation_metrics.driver_flags:
            review_focus.append("确认结果下降是否与执行偏离同步")
            initial_interventions.append("优先检查近期结果下降对应的训练环节")
        if gait_explanation.available:
            review_focus.append("结合步态补充数据确认动作质量变化")

        if not review_focus:
            review_focus.append("当前偏离较轻，建议常规跟踪")
        if not initial_interventions:
            initial_interventions.append("保持当前方案，持续监测下一个时间窗")

        placeholder_reflection = ReviewCard(
            patient_id=patient_id or plan_summary.patient_id or -1,
            therapist_id=therapist_id or plan_summary.therapist_id,
            primary_plan_id=plan_id or plan_summary.selected_plan_id,
            time_range=plan_summary.time_range,
            source_backend=self.repository.last_backend,
            plan_summary=plan_summary,
            execution_summary=execution_summary,
            deviation_metrics=deviation_metrics,
            outcome_change=outcome_change,
            gait_explanation=gait_explanation,
            review_focus=review_focus,
            initial_interventions=initial_interventions,
            reflection=self.reflection_service.reflect_on_output_placeholder(),
            narrative_summary="",
        )
        reflection = self.reflection_service.reflect_on_output(placeholder_reflection)
        narrative_summary = (
            f"患者 {patient_id or plan_summary.patient_id} 在 {plan_summary.time_range.label} 内"
            f"风险等级为 {deviation_metrics.risk_level}，"
            f"{deviation_metrics.summary_text} {outcome_change.summary_text}"
        )
        return ReviewCard(
            patient_id=patient_id or plan_summary.patient_id or -1,
            therapist_id=therapist_id or plan_summary.therapist_id,
            primary_plan_id=plan_id or plan_summary.selected_plan_id,
            time_range=plan_summary.time_range,
            source_backend=self.repository.last_backend,
            plan_summary=plan_summary,
            execution_summary=execution_summary,
            deviation_metrics=deviation_metrics,
            outcome_change=outcome_change,
            gait_explanation=gait_explanation,
            review_focus=review_focus,
            initial_interventions=initial_interventions,
            reflection=reflection,
            narrative_summary=narrative_summary,
        )

    def screen_risk_patients(
        self,
        *,
        therapist_id: int,
        days: int = 7,
        start: datetime | None = None,
        end: datetime | None = None,
        top_k: int = 10,
    ) -> list[PatientRiskSummary]:
        report = self.generate_weekly_risk_report(
            therapist_id=therapist_id,
            days=days,
            start=start,
            end=end,
            top_k=top_k,
        )
        return report.patients

    def generate_weekly_risk_report(
        self,
        *,
        therapist_id: int,
        days: int = 7,
        start: datetime | None = None,
        end: datetime | None = None,
        top_k: int = 10,
    ) -> WeeklyRiskReport:
        time_range = build_time_range(
            self.repository,
            therapist_id=therapist_id,
            days=days,
            start=start,
            end=end,
        )
        plans = self.repository.get_plan_records(
            therapist_id=therapist_id,
            start=time_range.start,
            end=time_range.end,
            limit=2000,
        )
        patient_ids = []
        for row in plans:
            patient_id = row["UserId"]
            if patient_id not in patient_ids:
                patient_ids.append(patient_id)

        patient_summaries: list[PatientRiskSummary] = []
        for patient_id in patient_ids[:30]:
            review_card = self.generate_review_card(
                patient_id=patient_id,
                therapist_id=therapist_id,
                days=days,
                start=time_range.start,
                end=time_range.end,
            )
            priority = "high" if review_card.deviation_metrics.risk_level == "high" else "normal"
            patient_summaries.append(
                PatientRiskSummary(
                    patient_id=review_card.patient_id,
                    therapist_id=review_card.therapist_id,
                    latest_plan_id=review_card.plan_summary.latest_plan_id,
                    risk_level=review_card.deviation_metrics.risk_level,
                    risk_score=review_card.deviation_metrics.risk_score,
                    recent_attendance_rate=review_card.deviation_metrics.attendance_rate,
                    recent_completion_rate=review_card.deviation_metrics.completion_rate,
                    recent_dose_adherence_rate=review_card.deviation_metrics.dose_adherence_rate,
                    interruption_risk_score=review_card.deviation_metrics.interruption_risk_score,
                    outcome_trend=review_card.outcome_change.trend_label,
                    review_priority=priority,
                    summary=review_card.narrative_summary,
                )
            )

        patient_summaries.sort(key=lambda item: item.risk_score, reverse=True)
        selected = patient_summaries[:top_k]
        high_risk_count = sum(1 for item in patient_summaries if item.risk_level == "high")
        medium_risk_count = sum(1 for item in patient_summaries if item.risk_level == "medium")
        low_risk_count = sum(1 for item in patient_summaries if item.risk_level == "low")
        deviation_statistics = {
            "avg_attendance_rate": round(sum(item.recent_attendance_rate for item in patient_summaries) / len(patient_summaries), 4)
            if patient_summaries
            else 0.0,
            "avg_completion_rate": round(sum(item.recent_completion_rate for item in patient_summaries) / len(patient_summaries), 4)
            if patient_summaries
            else 0.0,
            "avg_dose_adherence_rate": round(sum(item.recent_dose_adherence_rate for item in patient_summaries) / len(patient_summaries), 4)
            if patient_summaries
            else 0.0,
        }
        outcome_statistics = {
            "declining_patients": float(sum(1 for item in patient_summaries if item.outcome_trend == "declining")),
            "stable_or_improving_patients": float(sum(1 for item in patient_summaries if item.outcome_trend != "declining")),
        }
        summary_text = (
            f"治疗师 {therapist_id} 在 {time_range.label} 内覆盖 {len(patient_summaries)} 名患者，"
            f"其中高风险 {high_risk_count} 名。"
        )
        return WeeklyRiskReport(
            therapist_id=therapist_id,
            time_range=time_range,
            source_backend=self.repository.last_backend,
            patient_count=len(patient_summaries),
            high_risk_count=high_risk_count,
            medium_risk_count=medium_risk_count,
            low_risk_count=low_risk_count,
            deviation_statistics=deviation_statistics,
            outcome_statistics=outcome_statistics,
            patients=selected,
            priority_patient_ids=[item.patient_id for item in selected[:3]],
            summary_text=summary_text,
            generated_at=datetime.now(),
        )
