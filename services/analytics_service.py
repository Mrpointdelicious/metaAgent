from __future__ import annotations

from datetime import datetime, time
from uuid import uuid4

from config import Settings
from models import AnalyticsResultRow, LastVisitInfo, PatientSet, PlanStatus, RankedPatientRow, RankedPatients
from repositories import RehabRepository

from .shared import parse_datetime_flexible


class AnalyticsService:
    def __init__(self, repository: RehabRepository, settings: Settings):
        self.repository = repository
        self.settings = settings
        self._set_registry: dict[str, PatientSet] = {}
        self._last_visit_cache: dict[int, LastVisitInfo] = {}
        self._plan_status_cache: dict[tuple[int, int | None, str | None, str | None], PlanStatus] = {}

    def list_patients_seen_by_doctor(
        self,
        doctor_id: int,
        start_date: str | None,
        end_date: str | None,
        source: str = "attendance",
    ) -> PatientSet:
        start_dt = self._parse_date(start_date, end_of_day=False)
        end_dt = self._parse_date(end_date, end_of_day=True)
        rows = self.repository.get_patients_seen_by_doctor(
            doctor_id=doctor_id,
            start=start_dt,
            end=end_dt,
            source=source,
        )
        patient_ids = sorted({int(row["patient_id"]) for row in rows if row.get("patient_id") is not None})
        description = self._describe_window(
            prefix=f"医生 {doctor_id} 在指定时间窗内实际到训过的患者集合",
            start_date=start_date,
            end_date=end_date,
        )
        note = None if patient_ids else "当前条件下未找到到训患者。"
        return self._register_set(
            patient_ids=patient_ids,
            description=description,
            note=note,
        )

    def list_patients_with_active_plans(
        self,
        doctor_id: int,
        start_date: str | None,
        end_date: str | None,
    ) -> PatientSet:
        start_dt = self._parse_date(start_date, end_of_day=False)
        end_dt = self._parse_date(end_date, end_of_day=True)
        rows = self.repository.get_patients_with_active_plans(
            doctor_id=doctor_id,
            start=start_dt,
            end=end_dt,
        )
        patient_ids = sorted({int(row["patient_id"]) for row in rows if row.get("patient_id") is not None})
        description = self._describe_window(
            prefix=f"医生 {doctor_id} 在指定时间窗内存在计划的患者集合",
            start_date=start_date,
            end_date=end_date,
        )
        note = None if patient_ids else "当前条件下未找到活跃计划患者。"
        return self._register_set(
            patient_ids=patient_ids,
            description=description,
            note=note,
        )

    def diff_patient_sets(self, base_set_id: str, subtract_set_id: str) -> PatientSet:
        base = self._set_registry.get(base_set_id)
        subtract = self._set_registry.get(subtract_set_id)
        if base is None:
            raise ValueError(f"unknown_set:{base_set_id}")
        if subtract is None:
            raise ValueError(f"unknown_set:{subtract_set_id}")
        patient_ids = sorted(set(base.patient_ids) - set(subtract.patient_ids))
        description = f"{base.description or base.set_id} 减去 {subtract.description or subtract.set_id}"
        note = None if patient_ids else "差集为空。"
        return self._register_set(
            patient_ids=patient_ids,
            description=description,
            note=note,
        )

    def get_patient_last_visit(
        self,
        patient_id: int,
        doctor_id: int | None = None,
    ) -> LastVisitInfo:
        row = self.repository.get_patient_last_visit(
            patient_id=patient_id,
            doctor_id=doctor_id,
        )
        if row is None:
            result = LastVisitInfo(
                patient_id=patient_id,
                doctor_id=doctor_id,
                note="未找到到训记录。",
            )
            self._last_visit_cache[patient_id] = result
            return result
        result = LastVisitInfo(
            patient_id=patient_id,
            doctor_id=row.get("doctor_id"),
            last_visit_time=self._to_iso(row.get("last_visit_time")),
            last_plan_id=row.get("last_plan_id"),
            last_device_id=row.get("last_device_id"),
            last_task_name=row.get("last_task_name"),
            note="最近一次到训来自执行日志摘要。",
        )
        self._last_visit_cache[patient_id] = result
        return result

    def get_patient_plan_status(
        self,
        patient_id: int,
        doctor_id: int | None,
        start_date: str,
        end_date: str,
    ) -> PlanStatus:
        start_dt = self._parse_date(start_date, end_of_day=False)
        end_dt = self._parse_date(end_date, end_of_day=True)
        row = self.repository.get_patient_plan_status(
            patient_id=patient_id,
            doctor_id=doctor_id,
            start=start_dt,
            end=end_dt,
        )
        planned_sessions = int(row.get("planned_sessions") or 0)
        attended_sessions = int(row.get("attended_sessions") or 0)
        missed = int(row.get("missed_planned_sessions") or 0)
        note_parts: list[str] = []
        if planned_sessions == 0:
            note_parts.append("窗口内没有计划。")
        elif attended_sessions == 0:
            note_parts.append("窗口内有计划但没有检测到实际到训。")
        elif missed > 0:
            note_parts.append("窗口内存在部分应训未训。")
        else:
            note_parts.append("窗口内计划与到训基本一致。")
        result = PlanStatus(
            patient_id=patient_id,
            doctor_id=doctor_id,
            window_start=start_date,
            window_end=end_date,
            has_active_plan=bool(row.get("has_active_plan")),
            planned_sessions=planned_sessions,
            attended_sessions=attended_sessions,
            missed_planned_sessions=missed,
            latest_plan_time=self._to_iso(row.get("latest_plan_time")),
            note=" ".join(note_parts),
        )
        self._plan_status_cache[(patient_id, doctor_id, start_date, end_date)] = result
        return result

    def rank_patients(
        self,
        patient_ids: list[int],
        strategy: str,
        top_k: int | None = None,
    ) -> RankedPatients:
        unique_ids = list(dict.fromkeys(patient_ids))
        rows: list[RankedPatientRow] = []
        if strategy == "active_plan_but_absent":
            sortable: list[tuple[int, float, str]] = []
            for patient_id in unique_ids:
                plan_status = self._find_latest_plan_status(patient_id)
                last_visit = self._last_visit_cache.get(patient_id)
                has_active_plan = bool(plan_status and plan_status.has_active_plan)
                missed_sessions = float(plan_status.missed_planned_sessions or 0)
                last_visit_dt = parse_datetime_flexible(last_visit.last_visit_time if last_visit else None)
                age_score = float((datetime.now() - last_visit_dt).days) if last_visit_dt else 0.0
                score = (1000.0 if has_active_plan else 0.0) + missed_sessions * 10.0 + age_score
                reason = "窗口内有计划但未到训" if has_active_plan else "最近未到训，且窗口内未发现计划"
                sortable.append((patient_id, score, reason))
            sortable.sort(key=lambda item: item[1], reverse=True)
            rows = [
                RankedPatientRow(patient_id=patient_id, rank_score=round(score, 2), rank_reason=reason)
                for patient_id, score, reason in sortable
            ]
            note = "优先按“窗口内仍有计划但未到训”排序，其次参考最近一次到训距今时间。"
        elif strategy == "last_visit_oldest":
            sortable = []
            for patient_id in unique_ids:
                last_visit = self._last_visit_cache.get(patient_id)
                last_visit_dt = parse_datetime_flexible(last_visit.last_visit_time if last_visit else None)
                score = float((datetime.now() - last_visit_dt).days) if last_visit_dt else float("inf")
                reason = "最近一次到训时间更早"
                sortable.append((patient_id, score, reason))
            sortable.sort(key=lambda item: item[1], reverse=True)
            rows = [
                RankedPatientRow(
                    patient_id=patient_id,
                    rank_score=None if score == float("inf") else round(score, 2),
                    rank_reason=reason,
                )
                for patient_id, score, reason in sortable
            ]
            note = "按最近一次到训时间由远到近排序。"
        elif strategy == "highest_risk":
            rows = [
                RankedPatientRow(
                    patient_id=patient_id,
                    rank_score=None,
                    rank_reason="MVP 尚未接入统一风险快照，当前按输入顺序保留。",
                )
                for patient_id in unique_ids
            ]
            note = "最高风险排序当前为占位实现。"
        else:
            raise ValueError(f"unsupported_strategy:{strategy}")

        if top_k is not None:
            rows = rows[:top_k]
        return RankedPatients(
            rows=rows,
            strategy=strategy,
            note=note,
        )

    def build_result_rows(self, ranked_patients: RankedPatients) -> list[AnalyticsResultRow]:
        result_rows: list[AnalyticsResultRow] = []
        for row in ranked_patients.rows:
            patient_id = row.patient_id
            last_visit = self._last_visit_cache.get(patient_id)
            plan_status = self._find_latest_plan_status(patient_id)
            notes = [item for item in [plan_status.note if plan_status else None, last_visit.note if last_visit else None] if item]
            result_rows.append(
                AnalyticsResultRow(
                    patient_id=patient_id,
                    last_visit_time=last_visit.last_visit_time if last_visit else None,
                    has_active_plan_in_window=plan_status.has_active_plan if plan_status else None,
                    planned_sessions=plan_status.planned_sessions if plan_status else None,
                    attended_sessions=plan_status.attended_sessions if plan_status else None,
                    missed_planned_sessions=plan_status.missed_planned_sessions if plan_status else None,
                    rank_score=row.rank_score,
                    rank_reason=row.rank_reason,
                    note=" ".join(notes) if notes else None,
                )
            )
        return result_rows

    def _register_set(
        self,
        *,
        patient_ids: list[int],
        description: str,
        note: str | None,
    ) -> PatientSet:
        patient_ids = list(dict.fromkeys(patient_ids))
        patient_set = PatientSet(
            set_id=f"set_{uuid4().hex[:10]}",
            patient_ids=patient_ids,
            count=len(patient_ids),
            description=description,
            source_backend=self.repository.last_backend,
            note=note,
        )
        self._set_registry[patient_set.set_id] = patient_set
        return patient_set

    def _find_latest_plan_status(self, patient_id: int) -> PlanStatus | None:
        candidates = [item for key, item in self._plan_status_cache.items() if key[0] == patient_id]
        return candidates[-1] if candidates else None

    def _describe_window(self, *, prefix: str, start_date: str | None, end_date: str | None) -> str:
        if start_date and end_date:
            return f"{prefix}（{start_date} 至 {end_date}）"
        if end_date and not start_date:
            return f"{prefix}（截至 {end_date}）"
        if start_date and not end_date:
            return f"{prefix}（自 {start_date} 起）"
        return prefix

    def _parse_date(self, value: str | None, *, end_of_day: bool) -> datetime | None:
        dt = parse_datetime_flexible(value)
        if dt is None:
            return None
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            if end_of_day:
                return datetime.combine(dt.date(), time.max).replace(microsecond=0)
            return datetime.combine(dt.date(), time.min)
        return dt

    def _to_iso(self, value: datetime | str | None) -> str | None:
        dt = parse_datetime_flexible(value)
        return dt.isoformat(sep=" ") if dt else None
