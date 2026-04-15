from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

from config import Settings
from models import ExecutionLog, ExecutionSummary, PlanSummary
from repositories import RehabRepository

from .shared import build_time_range, format_number, parse_datetime_flexible


class ExecutionService:
    def __init__(self, repository: RehabRepository, settings: Settings):
        self.repository = repository
        self.settings = settings

    def get_execution_logs(
        self,
        *,
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        plan_summary: PlanSummary | None = None,
    ) -> ExecutionSummary:
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

        rows = self.repository.get_execution_logs(
            patient_id=patient_id,
            therapist_id=therapist_id,
            plan_ids=plan_ids,
            start=time_range.start,
            end=time_range.end,
        )

        logs: list[ExecutionLog] = []
        duration_minutes_by_plan: dict[int, float] = defaultdict(float)
        log_count_by_plan: dict[int, int] = defaultdict(int)
        task_counter: Counter[str] = Counter()

        for row in rows:
            duration_seconds = float(row.get("Duration") or 0.0)
            duration_minutes = duration_seconds / 60.0
            task_name = row.get("Name")
            log = ExecutionLog(
                log_id=row["Id"],
                plan_id=row.get("PlanId"),
                patient_id=row.get("UserId"),
                therapist_id=row.get("DoctorId"),
                task_name=task_name,
                device_id=row.get("DeviceId"),
                start_time=parse_datetime_flexible(row.get("StartTime")),
                end_time=parse_datetime_flexible(row.get("EndTime")),
                duration_seconds=duration_seconds,
                duration_minutes=round(duration_minutes, 2),
                is_complete=row.get("IsComplete"),
                score=row.get("Score"),
                task_type=row.get("Type"),
            )
            logs.append(log)
            if log.plan_id is not None:
                duration_minutes_by_plan[log.plan_id] += duration_minutes
                log_count_by_plan[log.plan_id] += 1
            task_counter[task_name or "unknown"] += 1

        logs.sort(key=lambda item: item.start_time or datetime.min, reverse=True)
        missing_notes: list[str] = []
        if not logs:
            missing_notes.append("未找到执行日志。")

        total_duration_minutes = round(sum(duration_minutes_by_plan.values()), 2)
        summary_text = (
            f"时间窗内共 {len(logs)} 条执行日志，累计执行 {format_number(total_duration_minutes)} 分钟，"
            f"覆盖 {len(duration_minutes_by_plan)} 个计划。"
        )
        return ExecutionSummary(
            time_range=time_range,
            source_backend=self.repository.last_backend,
            log_count=len(logs),
            total_duration_minutes=total_duration_minutes,
            unique_plan_ids=sorted(duration_minutes_by_plan.keys()),
            logs=logs,
            log_count_by_plan={key: value for key, value in log_count_by_plan.items()},
            duration_minutes_by_plan={key: round(value, 2) for key, value in duration_minutes_by_plan.items()},
            task_count_by_name=dict(task_counter),
            missing_data_notes=missing_notes,
            summary_text=summary_text,
        )
