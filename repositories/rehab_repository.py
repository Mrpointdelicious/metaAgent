from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from config import Settings

from .db_client import DatabaseConnectionError, MySQLReadOnlyClient
from .mock_data import (
    get_mock_execution_rows,
    get_mock_plan_rows,
    get_mock_report_rows,
    get_mock_user_rows,
    get_mock_walk_detail_rows,
    get_mock_walk_rows,
)


class RehabRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = MySQLReadOnlyClient(settings)
        self.last_backend = "mysql"

    def _run_query(
        self,
        sql: str,
        params: Iterable[Any],
        mock_loader,
        *,
        mock_kwargs: dict[str, Any],
    ) -> list[dict[str, Any]]:
        try:
            rows = self.client.query(sql, params)
            self.last_backend = "mysql"
            return rows
        except DatabaseConnectionError:
            if not self.settings.use_mock_when_db_unavailable:
                raise
            self.last_backend = "mock"
            return mock_loader(**mock_kwargs)

    def get_plan_anchor(self, *, patient_id: int | None = None, therapist_id: int | None = None) -> datetime | None:
        sql = """
        SELECT MAX(COALESCE(BookingTime, CreateTime)) AS latest_anchor
        FROM dbrehaplan
        WHERE 1 = 1
        """
        params: list[Any] = []
        if patient_id is not None:
            sql += " AND UserId = %s"
            params.append(patient_id)
        if therapist_id is not None:
            sql += " AND DoctorId = %s"
            params.append(therapist_id)
        rows = self._run_query(
            sql,
            params,
            get_mock_plan_rows,
            mock_kwargs={"patient_id": patient_id, "therapist_id": therapist_id},
        )
        if self.last_backend == "mock":
            anchors = [row["BookingTime"] or row["CreateTime"] for row in rows]
            return max(anchors) if anchors else None
        return rows[0]["latest_anchor"] if rows else None

    def get_users_by_ids(self, user_ids: Iterable[int]) -> list[dict[str, Any]]:
        unique_ids = sorted({int(item) for item in user_ids if item is not None})
        if not unique_ids:
            return []
        placeholders = ",".join(["%s"] * len(unique_ids))
        sql = f"""
        SELECT
            Id AS user_id,
            NULLIF(TRIM(Name), '') AS user_name
        FROM dbuser
        WHERE Id IN ({placeholders})
        """
        rows = self._run_query(
            sql,
            unique_ids,
            get_mock_user_rows,
            mock_kwargs={"user_ids": unique_ids},
        )
        normalized: list[dict[str, Any]] = []
        for row in rows:
            user_id = row.get("user_id", row.get("Id"))
            if user_id is None:
                continue
            name = row.get("user_name", row.get("Name"))
            normalized.append(
                {
                    "user_id": int(user_id),
                    "user_name": str(name).strip() if name is not None and str(name).strip() else None,
                }
            )
        return normalized

    def get_user_name_map(self, user_ids: Iterable[int]) -> dict[int, str]:
        name_map: dict[int, str] = {}
        for row in self.get_users_by_ids(user_ids):
            user_id = row.get("user_id")
            user_name = row.get("user_name")
            if user_id is None or not user_name:
                continue
            name_map[int(user_id)] = str(user_name)
        return name_map

    def get_walk_anchor(self, *, patient_id: int | None = None) -> datetime | None:
        sql = "SELECT MAX(createTime) AS latest_anchor FROM dbwalk WHERE 1 = 1"
        params: list[Any] = []
        if patient_id is not None:
            sql += " AND userId = %s"
            params.append(patient_id)
        rows = self._run_query(
            sql,
            params,
            get_mock_walk_rows,
            mock_kwargs={"patient_id": patient_id},
        )
        if self.last_backend == "mock":
            anchors = [row["createTime"] for row in rows]
            return max(anchors) if anchors else None
        return rows[0]["latest_anchor"] if rows else None

    def get_plan_records(
        self,
        *,
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        sql = """
        SELECT
            p.*,
            t.Title AS template_title,
            t.Details AS template_details,
            t.Duration AS template_duration,
            t.ModelType AS template_model_type
        FROM dbrehaplan p
        LEFT JOIN dbtemplates t ON p.TemplateId = t.Id
        WHERE 1 = 1
        """
        params: list[Any] = []
        if patient_id is not None:
            sql += " AND p.UserId = %s"
            params.append(patient_id)
        if plan_id is not None:
            sql += " AND p.Id = %s"
            params.append(plan_id)
        if therapist_id is not None:
            sql += " AND p.DoctorId = %s"
            params.append(therapist_id)
        if start is not None:
            sql += " AND COALESCE(p.BookingTime, p.CreateTime) >= %s"
            params.append(start)
        if end is not None:
            sql += " AND COALESCE(p.BookingTime, p.CreateTime) <= %s"
            params.append(end)
        sql += " ORDER BY COALESCE(p.BookingTime, p.CreateTime) DESC, p.Id DESC LIMIT %s"
        params.append(limit)
        return self._run_query(
            sql,
            params,
            get_mock_plan_rows,
            mock_kwargs={
                "patient_id": patient_id,
                "plan_id": plan_id,
                "therapist_id": therapist_id,
                "start": start,
                "end": end,
            },
        )

    def get_execution_logs(
        self,
        *,
        patient_id: int | None = None,
        therapist_id: int | None = None,
        plan_ids: Iterable[int] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        sql = """
        SELECT
            dl.*,
            p.UserId,
            p.DoctorId,
            p.BookingTime,
            p.CreateTime
        FROM dbdevicelog dl
        LEFT JOIN dbrehaplan p ON dl.PlanId = p.Id
        WHERE 1 = 1
        """
        params: list[Any] = []
        plan_ids = list(plan_ids or [])
        if patient_id is not None:
            sql += " AND p.UserId = %s"
            params.append(patient_id)
        if therapist_id is not None:
            sql += " AND p.DoctorId = %s"
            params.append(therapist_id)
        if plan_ids:
            placeholders = ",".join(["%s"] * len(plan_ids))
            sql += f" AND dl.PlanId IN ({placeholders})"
            params.extend(plan_ids)
        if start is not None:
            sql += " AND dl.StartTime >= %s"
            params.append(start)
        if end is not None:
            sql += " AND dl.StartTime <= %s"
            params.append(end)
        sql += " ORDER BY dl.StartTime DESC, dl.Id DESC LIMIT %s"
        params.append(limit)
        return self._run_query(
            sql,
            params,
            get_mock_execution_rows,
            mock_kwargs={
                "patient_id": patient_id,
                "therapist_id": therapist_id,
                "plan_ids": plan_ids,
                "start": start,
                "end": end,
            },
        )

    def get_reports(
        self,
        *,
        patient_id: int | None = None,
        therapist_id: int | None = None,
        plan_ids: Iterable[int] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        sql = """
        SELECT
            r.*,
            p.UserId,
            p.DoctorId,
            p.BookingTime
        FROM dbreport r
        LEFT JOIN dbrehaplan p ON r.planId = p.Id
        WHERE 1 = 1
        """
        params: list[Any] = []
        plan_ids = list(plan_ids or [])
        if patient_id is not None:
            sql += " AND p.UserId = %s"
            params.append(patient_id)
        if therapist_id is not None:
            sql += " AND p.DoctorId = %s"
            params.append(therapist_id)
        if plan_ids:
            placeholders = ",".join(["%s"] * len(plan_ids))
            sql += f" AND r.planId IN ({placeholders})"
            params.extend(plan_ids)
        if start is not None:
            sql += " AND r.CreateTime >= %s"
            params.append(start)
        if end is not None:
            sql += " AND r.CreateTime <= %s"
            params.append(end)
        sql += " ORDER BY r.CreateTime DESC, r.Id DESC LIMIT %s"
        params.append(limit)
        return self._run_query(
            sql,
            params,
            get_mock_report_rows,
            mock_kwargs={
                "patient_id": patient_id,
                "therapist_id": therapist_id,
                "plan_ids": plan_ids,
                "start": start,
                "end": end,
            },
        )

    def get_walk_sessions(
        self,
        *,
        patient_id: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = """
        SELECT
            id,
            startTime,
            endTime,
            deviceId,
            itemId,
            userId,
            details,
            status,
            gameStatus,
            createTime,
            duration
        FROM dbwalk
        WHERE 1 = 1
        """
        params: list[Any] = []
        if patient_id is not None:
            sql += " AND userId = %s"
            params.append(patient_id)
        if start is not None:
            sql += " AND createTime >= %s"
            params.append(start)
        if end is not None:
            sql += " AND createTime <= %s"
            params.append(end)
        sql += " ORDER BY createTime DESC, id DESC LIMIT %s"
        params.append(limit)
        return self._run_query(
            sql,
            params,
            get_mock_walk_rows,
            mock_kwargs={"patient_id": patient_id, "start": start, "end": end},
        )

    def get_walk_report_details(
        self,
        *,
        patient_id: int | None = None,
        walk_plan_ids: Iterable[int] | None = None,
    ) -> list[dict[str, Any]]:
        sql = """
        SELECT
            w.id AS walk_plan_id,
            w.itemId,
            w.userId,
            w.startTime,
            w.duration,
            w.details AS walk_details,
            wd.details AS report_details
        FROM dbwalk w
        LEFT JOIN walkreportdetails wd ON w.id = wd.planId
        WHERE 1 = 1
        """
        params: list[Any] = []
        walk_plan_ids = list(walk_plan_ids or [])
        if patient_id is not None:
            sql += " AND w.userId = %s"
            params.append(patient_id)
        if walk_plan_ids:
            placeholders = ",".join(["%s"] * len(walk_plan_ids))
            sql += f" AND w.id IN ({placeholders})"
            params.extend(walk_plan_ids)
        sql += " ORDER BY w.id DESC"
        return self._run_query(
            sql,
            params,
            get_mock_walk_detail_rows,
            mock_kwargs={"patient_id": patient_id, "walk_plan_ids": walk_plan_ids},
        )

    def get_patients_seen_by_doctor(
        self,
        *,
        doctor_id: int,
        start: datetime | None = None,
        end: datetime | None = None,
        source: str = "attendance",
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        if source != "attendance":
            return []
        rows = self.get_execution_logs(
            therapist_id=doctor_id,
            start=start,
            end=end,
            limit=limit,
        )
        patient_ids: list[int] = []
        for row in rows:
            patient_id = row.get("UserId")
            if patient_id is None or patient_id in patient_ids:
                continue
            patient_ids.append(patient_id)
        name_map = self.get_user_name_map(patient_ids)
        return [{"patient_id": patient_id, "patient_name": name_map.get(patient_id)} for patient_id in sorted(patient_ids)]

    def get_patients_with_active_plans(
        self,
        *,
        doctor_id: int,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        rows = self.get_plan_records(
            therapist_id=doctor_id,
            start=start,
            end=end,
            limit=limit,
        )
        grouped: dict[int, dict[str, Any]] = {}
        for row in rows:
            patient_id = row.get("UserId")
            if patient_id is None:
                continue
            anchor = row.get("BookingTime") or row.get("CreateTime")
            current = grouped.get(patient_id)
            if current is None:
                grouped[patient_id] = {
                    "patient_id": patient_id,
                    "plan_count": 1,
                    "latest_plan_time": anchor,
                }
                continue
            current["plan_count"] += 1
            if anchor and (current["latest_plan_time"] is None or anchor > current["latest_plan_time"]):
                current["latest_plan_time"] = anchor
        name_map = self.get_user_name_map(grouped.keys())
        for patient_id, item in grouped.items():
            item["patient_name"] = name_map.get(patient_id)
        return [grouped[key] for key in sorted(grouped)]

    def get_doctors_with_active_plans(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        rows = self.get_plan_records(
            start=start,
            end=end,
            limit=limit,
        )
        grouped: dict[int, dict[str, Any]] = {}
        for row in rows:
            doctor_id = row.get("DoctorId")
            patient_id = row.get("UserId")
            if doctor_id is None:
                continue
            current = grouped.get(doctor_id)
            if current is None:
                current = {
                    "doctor_id": int(doctor_id),
                    "active_plan_count": 0,
                    "patient_ids": set(),
                }
                grouped[int(doctor_id)] = current
            current["active_plan_count"] += 1
            if patient_id is not None:
                current["patient_ids"].add(int(patient_id))

        result_rows: list[dict[str, Any]] = []
        for doctor_id in sorted(grouped):
            item = grouped[doctor_id]
            result_rows.append(
                {
                    "doctor_id": doctor_id,
                    "active_plan_count": item["active_plan_count"],
                    "active_plan_patient_count": len(item["patient_ids"]),
                }
            )
        name_map = self.get_user_name_map(grouped.keys())
        for item in result_rows:
            item["doctor_name"] = name_map.get(item["doctor_id"])
        return result_rows

    def get_patient_last_visit(
        self,
        *,
        patient_id: int,
        doctor_id: int | None = None,
    ) -> dict[str, Any] | None:
        rows = self.get_execution_logs(
            patient_id=patient_id,
            therapist_id=doctor_id,
            limit=500,
        )
        if not rows:
            return None
        latest = max(rows, key=lambda item: item.get("StartTime") or datetime.min)
        latest_doctor_id = latest.get("DoctorId")
        name_map = self.get_user_name_map([patient_id, latest_doctor_id])
        return {
            "patient_id": patient_id,
            "patient_name": name_map.get(patient_id),
            "doctor_id": latest_doctor_id,
            "doctor_name": name_map.get(latest_doctor_id),
            "last_visit_time": latest.get("StartTime"),
            "last_plan_id": latest.get("PlanId"),
            "last_device_id": latest.get("DeviceId"),
            "last_task_name": latest.get("Name"),
        }

    def get_patient_plan_status(
        self,
        *,
        patient_id: int,
        doctor_id: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, Any]:
        plan_rows = self.get_plan_records(
            patient_id=patient_id,
            therapist_id=doctor_id,
            start=start,
            end=end,
            limit=1000,
        )
        plan_ids = [row["Id"] for row in plan_rows if row.get("Id") is not None]
        execution_rows = self.get_execution_logs(
            patient_id=patient_id,
            therapist_id=doctor_id,
            plan_ids=plan_ids,
            start=start,
            end=end,
            limit=2000,
        )
        attended_plan_ids = {
            row.get("PlanId")
            for row in execution_rows
            if row.get("PlanId") is not None
        }
        latest_plan_time = None
        for row in plan_rows:
            anchor = row.get("BookingTime") or row.get("CreateTime")
            if anchor and (latest_plan_time is None or anchor > latest_plan_time):
                latest_plan_time = anchor
        name_map = self.get_user_name_map([patient_id, doctor_id] if doctor_id is not None else [patient_id])
        return {
            "patient_id": patient_id,
            "patient_name": name_map.get(patient_id),
            "doctor_id": doctor_id,
            "doctor_name": name_map.get(doctor_id) if doctor_id is not None else None,
            "has_active_plan": bool(plan_rows),
            "planned_sessions": len(plan_rows),
            "attended_sessions": len(attended_plan_ids),
            "missed_planned_sessions": max(len(plan_rows) - len(attended_plan_ids), 0),
            "latest_plan_time": latest_plan_time,
        }
