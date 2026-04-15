from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from config import Settings

from .db_client import DatabaseConnectionError, MySQLReadOnlyClient
from .mock_data import (
    get_mock_execution_rows,
    get_mock_plan_rows,
    get_mock_report_rows,
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
