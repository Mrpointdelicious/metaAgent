from __future__ import annotations

from datetime import datetime

from config import Settings
from models import OutcomeChangeSummary, OutcomeReport, PlanSummary
from repositories import RehabRepository

from .shared import average, build_time_range, parse_report_entries, summarize_report_entries


class OutcomeService:
    def __init__(self, repository: RehabRepository, settings: Settings):
        self.repository = repository
        self.settings = settings

    def get_outcome_change(
        self,
        *,
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        plan_summary: PlanSummary | None = None,
    ) -> OutcomeChangeSummary:
        if plan_summary is not None:
            time_range = plan_summary.time_range
            plan_ids = [session.plan_id for session in plan_summary.sessions]
            patient_id = patient_id or plan_summary.patient_id
            therapist_id = therapist_id or plan_summary.therapist_id
        else:
            time_range = build_time_range(
                self.repository,
                patient_id=patient_id,
                therapist_id=therapist_id,
                days=days or self.settings.default_time_window_days,
                start=start,
                end=end,
            )
            plan_ids = [plan_id] if plan_id is not None else []

        rows = self.repository.get_reports(
            patient_id=patient_id,
            therapist_id=therapist_id,
            plan_ids=plan_ids,
            start=time_range.start,
            end=time_range.end,
        )
        reports: list[OutcomeReport] = []
        report_count_by_plan: dict[int, int] = {}
        training_minutes_by_plan: dict[int, float] = {}

        for row in rows:
            summary = summarize_report_entries(parse_report_entries(row.get("ReportDetails")))
            report = OutcomeReport(
                report_id=row["Id"],
                plan_id=row.get("planId"),
                patient_id=row.get("UserId"),
                therapist_id=row.get("DoctorId"),
                create_time=row.get("CreateTime"),
                health_score=row.get("HealthScore"),
                game_score=row.get("GameScore"),
                total_training_minutes=summary["total_training_minutes"],
                walk_distance=summary["walk_distance"],
                sit_count=summary["sit_count"],
                balance_time=summary["balance_time"],
                game_score_from_detail=summary["game_score"],
                detail_modes=summary["detail_modes"],
                raw_metrics=summary,
                highlight_text=(
                    f"训练 {summary['total_training_minutes']:.1f} 分钟，"
                    f"步行距离 {summary['walk_distance']:.1f}。"
                ),
            )
            reports.append(report)
            if report.plan_id is not None:
                report_count_by_plan[report.plan_id] = report_count_by_plan.get(report.plan_id, 0) + 1
                training_minutes_by_plan[report.plan_id] = max(
                    training_minutes_by_plan.get(report.plan_id, 0.0),
                    report.total_training_minutes,
                )

        reports.sort(key=lambda item: item.create_time or datetime.min, reverse=True)
        latest_group = reports[:3]
        baseline_group = reports[3:6] or reports[:3]

        latest_training = average([item.total_training_minutes for item in latest_group])
        baseline_training = average([item.total_training_minutes for item in baseline_group])
        latest_distance = average([item.walk_distance for item in latest_group])
        baseline_distance = average([item.walk_distance for item in baseline_group])
        latest_game = average([item.game_score_from_detail for item in latest_group])
        baseline_game = average([item.game_score_from_detail for item in baseline_group])

        training_delta = None if latest_training is None or baseline_training is None else round(latest_training - baseline_training, 2)
        distance_delta = None if latest_distance is None or baseline_distance is None else round(latest_distance - baseline_distance, 2)
        game_delta = None if latest_game is None or baseline_game is None else round(latest_game - baseline_game, 2)

        trend_label = "stable"
        if (training_delta or 0.0) < -1.0 or (distance_delta or 0.0) < -1.0 or (game_delta or 0.0) < -5.0:
            trend_label = "declining"
        elif (training_delta or 0.0) > 1.0 or (distance_delta or 0.0) > 1.0 or (game_delta or 0.0) > 5.0:
            trend_label = "improving"

        missing_notes: list[str] = []
        if not reports:
            missing_notes.append("未找到结果报告。")
        summary_text = (
            f"时间窗内共 {len(reports)} 份结果报告，趋势判断为 {trend_label}，"
            f"最近训练时长均值 {latest_training if latest_training is not None else 0.0:.1f} 分钟。"
        )
        return OutcomeChangeSummary(
            time_range=time_range,
            source_backend=self.repository.last_backend,
            report_count=len(reports),
            reports=reports,
            report_count_by_plan=report_count_by_plan,
            training_minutes_by_plan=training_minutes_by_plan,
            latest_training_minutes=latest_training,
            baseline_training_minutes=baseline_training,
            training_minutes_delta=training_delta,
            latest_walk_distance=latest_distance,
            baseline_walk_distance=baseline_distance,
            walk_distance_delta=distance_delta,
            latest_game_score=latest_game,
            baseline_game_score=baseline_game,
            game_score_delta=game_delta,
            trend_label=trend_label,
            missing_data_notes=missing_notes,
            summary_text=summary_text,
        )
