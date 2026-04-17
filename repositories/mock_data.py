from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Iterable


MOCK_PLAN_ROWS: list[dict[str, Any]] = [
    {
        "Id": 9001,
        "TemplateId": 5001,
        "BusinessId": 1,
        "CreateTime": datetime(2025, 9, 10, 9, 0),
        "IsComplete": 1,
        "DoctorId": 30001,
        "UserId": 20001,
        "BookingTime": datetime(2025, 9, 10, 0, 0),
        "Duration": 20,
        "Details": '[{"gameName":"Walk","GameNameCh":"步行训练","GameCh":"行走训练","time":10,"speed":20,"assistance":0,"resistance":0,"templateMode":0},{"gameName":"Sit","GameNameCh":"坐站","GameCh":"坐站训练","time":10,"speed":0,"assistance":0,"resistance":0,"templateMode":1}]',
        "Reportlink": "9001.pdf",
        "Updatetime": datetime(2025, 9, 10, 9, 40),
        "Endtime": datetime(2025, 9, 10, 9, 38),
        "Deviceid": 11,
        "StartTime": datetime(1900, 1, 1, 0, 0),
        "Status": 1,
        "template_title": "基础步行模板",
        "template_details": '[{"gameName":"Walk","GameNameCh":"步行训练","GameCh":"行走训练","time":10,"speed":20,"assistance":0,"resistance":0,"templateMode":0},{"gameName":"Sit","GameNameCh":"坐站","GameCh":"坐站训练","time":10,"speed":0,"assistance":0,"resistance":0,"templateMode":1}]',
        "template_duration": 20,
        "template_model_type": 1,
    },
    {
        "Id": 9002,
        "TemplateId": 5001,
        "BusinessId": 1,
        "CreateTime": datetime(2025, 9, 12, 9, 0),
        "IsComplete": 0,
        "DoctorId": 30001,
        "UserId": 20001,
        "BookingTime": datetime(2025, 9, 12, 0, 0),
        "Duration": 20,
        "Details": '[{"gameName":"Walk","GameNameCh":"步行训练","GameCh":"行走训练","time":10,"speed":20,"assistance":0,"resistance":0,"templateMode":0},{"gameName":"Sit","GameNameCh":"坐站","GameCh":"坐站训练","time":10,"speed":0,"assistance":0,"resistance":0,"templateMode":1}]',
        "Reportlink": None,
        "Updatetime": datetime(2025, 9, 12, 9, 10),
        "Endtime": datetime(1900, 1, 1, 0, 0),
        "Deviceid": 11,
        "StartTime": datetime(1900, 1, 1, 0, 0),
        "Status": 0,
        "template_title": "基础步行模板",
        "template_details": '[{"gameName":"Walk","GameNameCh":"步行训练","GameCh":"行走训练","time":10,"speed":20,"assistance":0,"resistance":0,"templateMode":0},{"gameName":"Sit","GameNameCh":"坐站","GameCh":"坐站训练","time":10,"speed":0,"assistance":0,"resistance":0,"templateMode":1}]',
        "template_duration": 20,
        "template_model_type": 1,
    },
    {
        "Id": 9003,
        "TemplateId": 5002,
        "BusinessId": 1,
        "CreateTime": datetime(2025, 9, 11, 14, 0),
        "IsComplete": 1,
        "DoctorId": 30001,
        "UserId": 20002,
        "BookingTime": datetime(2025, 9, 11, 0, 0),
        "Duration": 15,
        "Details": '[{"gameName":"Walk","GameNameCh":"步行训练","GameCh":"行走训练","time":15,"speed":10,"assistance":5,"resistance":0,"templateMode":0}]',
        "Reportlink": "9003.pdf",
        "Updatetime": datetime(2025, 9, 11, 14, 30),
        "Endtime": datetime(2025, 9, 11, 14, 28),
        "Deviceid": 12,
        "StartTime": datetime(1900, 1, 1, 0, 0),
        "Status": 1,
        "template_title": "减量训练模板",
        "template_details": '[{"gameName":"Walk","GameNameCh":"步行训练","GameCh":"行走训练","time":15,"speed":10,"assistance":5,"resistance":0,"templateMode":0}]',
        "template_duration": 15,
        "template_model_type": 1,
    },
]

MOCK_EXECUTION_ROWS: list[dict[str, Any]] = [
    {"Id": 8001, "Name": "walk", "DeviceId": 11, "PlanId": 9001, "Duration": 480.0, "StartTime": datetime(2025, 9, 10, 9, 2), "EndTime": datetime(2025, 9, 10, 9, 10), "IsComplete": 1, "Score": 0, "Type": 0, "UserId": 20001, "DoctorId": 30001},
    {"Id": 8002, "Name": "sitstand", "DeviceId": 11, "PlanId": 9001, "Duration": 420.0, "StartTime": datetime(2025, 9, 10, 9, 12), "EndTime": datetime(2025, 9, 10, 9, 19), "IsComplete": 1, "Score": 0, "Type": 1, "UserId": 20001, "DoctorId": 30001},
    {"Id": 8003, "Name": "walk", "DeviceId": 12, "PlanId": 9003, "Duration": 300.0, "StartTime": datetime(2025, 9, 11, 14, 5), "EndTime": datetime(2025, 9, 11, 14, 10), "IsComplete": 1, "Score": 10, "Type": 0, "UserId": 20002, "DoctorId": 30001},
]

MOCK_REPORT_ROWS: list[dict[str, Any]] = [
    {"Id": 7001, "ReportDetails": '{"ReportList":[{"ReportMode":1,"WTime":10.0,"WDistance":25.0,"WTrainingWalktime":8.0},{"ReportMode":2,"STime":10.0,"SNumber":8,"STrainingSitTime":7.0}],"ReportProcess":"12"}', "HealthScore": 0, "GameScore": 0, "CreateTime": datetime(2025, 9, 10, 9, 35), "UpdateTime": datetime(2025, 9, 10, 9, 35), "planId": 9001, "UserId": 20001, "DoctorId": 30001},
    {"Id": 7002, "ReportDetails": '{"ReportList":[{"ReportMode":1,"WTime":15.0,"WDistance":8.0,"WTrainingWalktime":5.0}],"ReportProcess":"1"}', "HealthScore": 0, "GameScore": 0, "CreateTime": datetime(2025, 9, 11, 14, 20), "UpdateTime": datetime(2025, 9, 11, 14, 20), "planId": 9003, "UserId": 20002, "DoctorId": 30001},
]

MOCK_WALK_ROWS: list[dict[str, Any]] = [
    {"id": 6001, "startTime": "2025/09/10 15:00:00", "endTime": "2025/09/10 15:03:00", "deviceId": 21, "itemId": 0, "userId": 20001, "details": '{"g_strightSpeed": 1.1, "g_trainingHours": 2, "g_strightVolume": 10}', "status": 1, "gameStatus": 1, "createTime": datetime(2025, 9, 10, 15, 0), "duration": 180},
]

MOCK_WALK_DETAIL_ROWS: list[dict[str, Any]] = [
    {"walk_plan_id": 6001, "itemId": 0, "userId": 20001, "startTime": "2025/09/10 15:00:00", "duration": 180, "walk_details": '{"g_strightSpeed": 1.1, "g_trainingHours": 2, "g_strightVolume": 10}', "report_details": '{"distance": 22.0, "avg_Speed": 1.05, "completionRate": 0.92, "correctRate": 0.88, "errorRate": 0.08}'},
]

MOCK_USER_ROWS: list[dict[str, Any]] = [
    {"Id": 56, "Name": "Demo Doctor 56"},
    {"Id": 146, "Name": "Demo Patient 146"},
    {"Id": 20001, "Name": "Mock Patient Alpha"},
    {"Id": 20002, "Name": "Mock Patient Beta"},
    {"Id": 30001, "Name": "Mock Doctor One"},
]


def _within_range(value: datetime | None, start: datetime | None, end: datetime | None) -> bool:
    if value is None:
        return False
    if start and value < start:
        return False
    if end and value > end:
        return False
    return True


def get_mock_plan_rows(*, patient_id: int | None = None, plan_id: int | None = None, therapist_id: int | None = None, start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in MOCK_PLAN_ROWS:
        if patient_id is not None and row["UserId"] != patient_id:
            continue
        if plan_id is not None and row["Id"] != plan_id:
            continue
        if therapist_id is not None and row["DoctorId"] != therapist_id:
            continue
        anchor = row.get("BookingTime") or row.get("CreateTime")
        if (start or end) and not _within_range(anchor, start, end):
            continue
        rows.append(deepcopy(row))
    return rows


def get_mock_execution_rows(*, patient_id: int | None = None, therapist_id: int | None = None, plan_ids: Iterable[int] | None = None, start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
    plan_set = set(plan_ids or [])
    rows: list[dict[str, Any]] = []
    for row in MOCK_EXECUTION_ROWS:
        if patient_id is not None and row["UserId"] != patient_id:
            continue
        if therapist_id is not None and row["DoctorId"] != therapist_id:
            continue
        if plan_set and row["PlanId"] not in plan_set:
            continue
        if (start or end) and not _within_range(row.get("StartTime"), start, end):
            continue
        rows.append(deepcopy(row))
    return rows


def get_mock_report_rows(*, patient_id: int | None = None, therapist_id: int | None = None, plan_ids: Iterable[int] | None = None, start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
    plan_set = set(plan_ids or [])
    rows: list[dict[str, Any]] = []
    for row in MOCK_REPORT_ROWS:
        if patient_id is not None and row["UserId"] != patient_id:
            continue
        if therapist_id is not None and row["DoctorId"] != therapist_id:
            continue
        if plan_set and row["planId"] not in plan_set:
            continue
        if (start or end) and not _within_range(row.get("CreateTime"), start, end):
            continue
        rows.append(deepcopy(row))
    return rows


def get_mock_walk_rows(*, patient_id: int | None = None, start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in MOCK_WALK_ROWS:
        if patient_id is not None and row["userId"] != patient_id:
            continue
        if (start or end) and not _within_range(row.get("createTime"), start, end):
            continue
        rows.append(deepcopy(row))
    return rows


def get_mock_walk_detail_rows(*, patient_id: int | None = None, walk_plan_ids: Iterable[int] | None = None) -> list[dict[str, Any]]:
    plan_set = set(walk_plan_ids or [])
    rows: list[dict[str, Any]] = []
    for row in MOCK_WALK_DETAIL_ROWS:
        if patient_id is not None and row["userId"] != patient_id:
            continue
        if plan_set and row["walk_plan_id"] not in plan_set:
            continue
        rows.append(deepcopy(row))
    return rows


def get_mock_user_rows(*, user_ids: Iterable[int] | None = None) -> list[dict[str, Any]]:
    id_set = {int(item) for item in (user_ids or []) if item is not None}
    if not id_set:
        return []
    return [deepcopy(row) for row in MOCK_USER_ROWS if int(row["Id"]) in id_set]
