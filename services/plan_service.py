from __future__ import annotations

from datetime import datetime

from config import Settings
from models import PlanSession, PlanSummary
from repositories import RehabRepository

from .shared import build_time_range, parse_datetime_flexible, parse_training_tasks, task_catalog


class PlanService:
    def __init__(self, repository: RehabRepository, settings: Settings):
        self.repository = repository
        self.settings = settings

    def get_plan_summary(
        self,
        *,
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> PlanSummary:
        if plan_id is not None:
            seed_rows = self.repository.get_plan_records(plan_id=plan_id, limit=1)
            if seed_rows:
                seed = seed_rows[0]
                patient_id = patient_id or seed.get("UserId")
                therapist_id = therapist_id or seed.get("DoctorId")
                anchor = parse_datetime_flexible(seed.get("BookingTime")) or parse_datetime_flexible(seed.get("CreateTime"))
                if anchor is not None:
                    end = end or anchor

        time_range = build_time_range(
            self.repository,
            patient_id=patient_id,
            therapist_id=therapist_id,
            days=days or self.settings.default_time_window_days,
            start=start,
            end=end,
        )
        rows = self.repository.get_plan_records(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            start=time_range.start,
            end=time_range.end,
        )

        sessions: list[PlanSession] = []
        all_task_labels: list[str] = []
        for row in rows:
            details_tasks = parse_training_tasks(row.get("Details"))
            template_tasks = parse_training_tasks(row.get("template_details"))
            planned_duration = row.get("Duration")
            if not planned_duration:
                planned_duration = sum(task.planned_time_min for task in details_tasks or template_tasks)
            session = PlanSession(
                plan_id=row["Id"],
                patient_id=row["UserId"],
                therapist_id=row.get("DoctorId"),
                template_id=row.get("TemplateId"),
                device_id=row.get("Deviceid"),
                booking_time=parse_datetime_flexible(row.get("BookingTime")),
                create_time=parse_datetime_flexible(row.get("CreateTime")),
                end_time=parse_datetime_flexible(row.get("Endtime")),
                planned_duration_min=float(planned_duration or 0.0),
                raw_is_complete=row.get("IsComplete"),
                raw_status=row.get("Status"),
                report_link=row.get("Reportlink"),
                details_tasks=details_tasks,
                template_tasks=template_tasks,
            )
            sessions.append(session)
            all_task_labels.extend(task_catalog(details_tasks or template_tasks))

        sessions.sort(
            key=lambda item: item.booking_time or item.create_time or datetime.min,
            reverse=True,
        )
        session_count = len(sessions)
        selected = next((item for item in sessions if item.plan_id == plan_id), None) if plan_id else (sessions[0] if sessions else None)
        latest = sessions[0] if sessions else None

        missing_notes: list[str] = []
        if not sessions:
            missing_notes.append("未在当前时间窗内找到计划记录。")
        if sessions and all(session.booking_time is None for session in sessions):
            missing_notes.append("计划缺少可信 BookingTime，仅能回退到 CreateTime。")

        planned_total_minutes = round(sum(session.planned_duration_min or 0.0 for session in sessions), 2)
        task_labels = sorted(set(filter(None, all_task_labels)))
        summary_text = (
            f"时间窗内共 {session_count} 个计划，计划总剂量 {planned_total_minutes:.1f} 分钟，"
            f"最近计划 ID 为 {latest.plan_id if latest else 'NA'}。"
        )
        return PlanSummary(
            patient_id=selected.patient_id if selected else patient_id,
            therapist_id=selected.therapist_id if selected else therapist_id,
            time_range=time_range,
            source_backend=self.repository.last_backend,
            session_count=session_count,
            selected_plan_id=selected.plan_id if selected else None,
            latest_plan_id=latest.plan_id if latest else None,
            planned_total_minutes=planned_total_minutes,
            tasks_catalog=task_labels,
            sessions=sessions,
            missing_data_notes=missing_notes,
            summary_text=summary_text,
        )
