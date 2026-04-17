from __future__ import annotations

from pydantic import BaseModel, Field


class PatientIdentity(BaseModel):
    patient_id: int
    patient_name: str | None = None


class PatientSet(BaseModel):
    set_id: str
    patient_ids: list[int] = Field(default_factory=list)
    patients: list[PatientIdentity] = Field(default_factory=list)
    patient_names: dict[int, str] = Field(default_factory=dict)
    count: int = 0
    description: str | None = None
    source_backend: str = ""
    note: str | None = None


class LastVisitInfo(BaseModel):
    patient_id: int
    patient_name: str | None = None
    doctor_id: int | None = None
    doctor_name: str | None = None
    last_visit_time: str | None = None
    last_plan_id: int | None = None
    last_device_id: int | None = None
    last_task_name: str | None = None
    note: str | None = None


class PlanStatus(BaseModel):
    patient_id: int
    patient_name: str | None = None
    doctor_id: int | None = None
    doctor_name: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    has_active_plan: bool = False
    planned_sessions: int | None = None
    attended_sessions: int | None = None
    missed_planned_sessions: int | None = None
    latest_plan_time: str | None = None
    note: str | None = None


class RankedPatientRow(BaseModel):
    patient_id: int
    patient_name: str | None = None
    rank_score: float | None = None
    rank_reason: str | None = None


class RankedPatients(BaseModel):
    rows: list[RankedPatientRow] = Field(default_factory=list)
    strategy: str
    note: str | None = None


class AnalyticsResultRow(BaseModel):
    patient_id: int
    patient_name: str | None = None
    doctor_id: int | None = None
    doctor_name: str | None = None
    last_visit_time: str | None = None
    has_active_plan_in_window: bool | None = None
    planned_sessions: int | None = None
    attended_sessions: int | None = None
    missed_planned_sessions: int | None = None
    rank_score: float | None = None
    rank_reason: str | None = None
    note: str | None = None


class DoctorAnalyticsResultRow(BaseModel):
    doctor_id: int
    doctor_name: str | None = None
    active_plan_patient_count: int = 0
    active_plan_count: int = 0
    note: str | None = None
