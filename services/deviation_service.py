from __future__ import annotations

from statistics import median

from config import Settings
from models import DeviationMetrics, ExecutionSummary, OutcomeChangeSummary, PlanSummary


class DeviationService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def calc_deviation_metrics(
        self,
        *,
        plan_summary: PlanSummary,
        execution_summary: ExecutionSummary,
        outcome_change: OutcomeChangeSummary | None = None,
    ) -> DeviationMetrics:
        sessions = sorted(
            plan_summary.sessions,
            key=lambda item: item.booking_time or item.create_time,
            reverse=True,
        )
        scheduled_sessions = len(sessions)
        if scheduled_sessions == 0:
            return DeviationMetrics(
                risk_level="high",
                risk_score=100.0,
                driver_flags=["no_plan_data"],
                summary_text="无计划记录，无法完成偏离计算。",
            )

        arrived_sessions = 0
        completed_sessions = 0
        planned_minutes: list[float] = []
        actual_minutes: list[float] = []
        dose_adherence_values: list[float] = []
        dose_deviation_values: list[float] = []
        arrived_dates = []

        report_count_by_plan = outcome_change.report_count_by_plan if outcome_change else {}
        report_minutes_by_plan = outcome_change.training_minutes_by_plan if outcome_change else {}

        recent_missed_streak = 0
        recent_window_flags: list[bool] = []
        latest_anchor = sessions[0].booking_time or sessions[0].create_time

        for session in sessions:
            plan_id = session.plan_id
            planned = session.planned_duration_min or 0.0
            execution_minutes = execution_summary.duration_minutes_by_plan.get(plan_id, 0.0)
            report_minutes = report_minutes_by_plan.get(plan_id, 0.0)
            actual = max(execution_minutes, report_minutes)

            has_report = report_count_by_plan.get(plan_id, 0) > 0
            arrived = has_report or execution_minutes > 0 or session.end_time is not None
            completed = bool(session.raw_is_complete == 1 or has_report or (session.end_time is not None and actual > 0))

            if arrived:
                arrived_sessions += 1
                arrived_date = session.booking_time or session.create_time
                if arrived_date is not None:
                    arrived_dates.append(arrived_date)
            if completed:
                completed_sessions += 1

            if planned > 0:
                planned_minutes.append(planned)
                actual_minutes.append(actual)
                raw_ratio = max(actual, 0.0) / planned
                dose_adherence_values.append(min(raw_ratio, 1.0))
                dose_deviation_values.append(min(abs(1.0 - raw_ratio), 1.0))

            if len(recent_window_flags) < 3:
                recent_window_flags.append(arrived)
            if not arrived and recent_missed_streak == len(recent_window_flags) - 1:
                recent_missed_streak += 1

        attendance_rate = arrived_sessions / scheduled_sessions if scheduled_sessions else 0.0
        completion_rate = completed_sessions / arrived_sessions if arrived_sessions else 0.0
        dose_adherence_rate = sum(dose_adherence_values) / len(dose_adherence_values) if dose_adherence_values else 0.0
        dose_deviation_rate = sum(dose_deviation_values) / len(dose_deviation_values) if dose_deviation_values else 1.0
        avg_planned_minutes = sum(planned_minutes) / len(planned_minutes) if planned_minutes else 0.0
        avg_actual_minutes = sum(actual_minutes) / len(actual_minutes) if actual_minutes else 0.0

        interruption_risk_score = recent_missed_streak * 25.0
        if recent_window_flags:
            recent_arrival_rate = sum(1 for flag in recent_window_flags if flag) / len(recent_window_flags)
            interruption_risk_score += (1.0 - recent_arrival_rate) * 30.0
        if len(arrived_dates) >= 2 and latest_anchor is not None:
            gaps = []
            for left, right in zip(arrived_dates[:-1], arrived_dates[1:]):
                if left and right:
                    gaps.append(abs((left - right).days) or 1)
            if gaps:
                baseline_gap = median(gaps)
                latest_arrived = max(arrived_dates)
                if latest_arrived and latest_anchor:
                    latest_gap = abs((latest_anchor - latest_arrived).days)
                    if latest_gap > baseline_gap * 1.5 and latest_gap > 3:
                        interruption_risk_score += 20.0
        interruption_risk_score = min(interruption_risk_score, 100.0)

        risk_score = (
            (1.0 - attendance_rate) * 40.0
            + (1.0 - completion_rate) * 20.0
            + dose_deviation_rate * 20.0
            + interruption_risk_score * 0.2
        )
        if outcome_change is not None:
            if outcome_change.trend_label == "declining":
                risk_score += 15.0
            elif outcome_change.trend_label == "improving":
                risk_score -= 5.0
        risk_score = max(0.0, min(risk_score, 100.0))

        if risk_score >= self.settings.high_risk_threshold:
            risk_level = "high"
        elif risk_score >= self.settings.medium_risk_threshold:
            risk_level = "medium"
        else:
            risk_level = "low"

        driver_flags: list[str] = []
        if attendance_rate < 0.7:
            driver_flags.append("low_attendance")
        if completion_rate < 0.7:
            driver_flags.append("low_completion")
        if dose_adherence_rate < 0.75:
            driver_flags.append("low_dose")
        if recent_missed_streak >= 2 or interruption_risk_score >= 50:
            driver_flags.append("interruption_risk")
        if outcome_change is not None and outcome_change.trend_label == "declining":
            driver_flags.append("outcome_declining")

        summary_text = (
            f"到训率 {attendance_rate * 100:.1f}%，完成率 {completion_rate * 100:.1f}%，"
            f"剂量达成 {dose_adherence_rate * 100:.1f}%，连续中断风险 {interruption_risk_score:.1f}。"
        )
        return DeviationMetrics(
            scheduled_sessions=scheduled_sessions,
            arrived_sessions=arrived_sessions,
            completed_sessions=completed_sessions,
            attendance_rate=round(attendance_rate, 4),
            completion_rate=round(completion_rate, 4),
            dose_adherence_rate=round(dose_adherence_rate, 4),
            dose_deviation_rate=round(dose_deviation_rate, 4),
            avg_planned_minutes=round(avg_planned_minutes, 2),
            avg_actual_minutes=round(avg_actual_minutes, 2),
            interruption_risk_score=round(interruption_risk_score, 2),
            consecutive_missed_sessions=recent_missed_streak,
            risk_score=round(risk_score, 2),
            risk_level=risk_level,
            driver_flags=driver_flags,
            summary_text=summary_text,
        )
