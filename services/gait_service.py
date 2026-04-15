from __future__ import annotations

from datetime import datetime

from config import Settings
from models import GaitExplanationSummary, GaitSessionExplanation
from repositories import RehabRepository

from .shared import build_time_range, parse_datetime_flexible, parse_json_field, safe_float


class GaitService:
    def __init__(self, repository: RehabRepository, settings: Settings):
        self.repository = repository
        self.settings = settings

    def get_gait_explanation(
        self,
        *,
        patient_id: int | None = None,
        item_id: int | None = None,
        days: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> GaitExplanationSummary:
        time_range = build_time_range(
            self.repository,
            patient_id=patient_id,
            days=days or self.settings.default_time_window_days,
            start=start,
            end=end,
            prefer_walk_anchor=True,
        )
        sessions = self.repository.get_walk_sessions(
            patient_id=patient_id,
            start=time_range.start,
            end=time_range.end,
        )
        if item_id is not None:
            sessions = [row for row in sessions if row.get("itemId") == item_id]
        walk_plan_ids = [row["id"] for row in sessions]
        detail_rows = self.repository.get_walk_report_details(
            patient_id=patient_id,
            walk_plan_ids=walk_plan_ids,
        )

        explanations: list[GaitSessionExplanation] = []
        for row in detail_rows:
            detail = parse_json_field(row.get("report_details")) or {}
            completion_rate = safe_float(detail.get("completionRate"), default=0.0) if detail else None
            correct_rate = safe_float(detail.get("correctRate"), default=0.0) if detail else None
            error_rate = safe_float(detail.get("errorRate"), default=0.0) if detail else None
            distance = safe_float(detail.get("distance"), default=0.0) if detail else None
            avg_speed = safe_float(detail.get("avg_Speed"), default=0.0) if detail else None
            explanation_parts: list[str] = []
            if completion_rate is not None:
                explanation_parts.append(f"完成率 {completion_rate * 100:.1f}%")
            if correct_rate is not None:
                explanation_parts.append(f"正确率 {correct_rate * 100:.1f}%")
            if distance is not None:
                explanation_parts.append(f"距离 {distance:.1f}")
            explanations.append(
                GaitSessionExplanation(
                    walk_plan_id=row["walk_plan_id"],
                    item_id=row.get("itemId"),
                    patient_id=row.get("userId"),
                    start_time=parse_datetime_flexible(row.get("startTime")),
                    duration_minutes=round(safe_float(row.get("duration")) / 60.0, 2),
                    completion_rate=completion_rate,
                    correct_rate=correct_rate,
                    error_rate=error_rate,
                    avg_speed=avg_speed,
                    distance=distance,
                    explanation="，".join(explanation_parts) if explanation_parts else "步态明细存在，但未抽取到稳定指标。",
                )
            )

        explanations.sort(key=lambda item: item.start_time or datetime.min, reverse=True)
        available = bool(explanations)
        note = (
            "步态解释来自独立步道产品链，仅作为补充解释，不直接参与 A 链偏离打分。"
            if available
            else "当前时间窗内未找到可用的步态增强数据。"
        )
        return GaitExplanationSummary(
            patient_id=patient_id,
            time_range=time_range,
            source_backend=self.repository.last_backend,
            available=available,
            note=note,
            sessions=explanations[:5],
        )
