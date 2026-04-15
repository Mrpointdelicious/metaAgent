from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .common import RiskLevel, TimeRange


class TrainingTask(BaseModel):
    task_code: str
    task_name: str | None = None
    category: str | None = None
    template_mode: int | None = None
    planned_time_min: float = 0.0
    speed: float | None = None
    assistance: float | None = None
    resistance: float | None = None
    sit_time: float | None = None
    stand_time: float | None = None
    weight_loss: float | None = None
    selected_index: int | None = None


class PlanSession(BaseModel):
    plan_id: int
    patient_id: int
    therapist_id: int | None = None
    template_id: int | None = None
    device_id: int | None = None
    booking_time: datetime | None = None
    create_time: datetime | None = None
    end_time: datetime | None = None
    planned_duration_min: float | None = None
    raw_is_complete: int | None = None
    raw_status: int | None = None
    report_link: str | None = None
    details_tasks: list[TrainingTask] = Field(default_factory=list)
    template_tasks: list[TrainingTask] = Field(default_factory=list)


class PlanSummary(BaseModel):
    patient_id: int | None = None
    therapist_id: int | None = None
    time_range: TimeRange
    source_backend: str
    session_count: int = 0
    selected_plan_id: int | None = None
    latest_plan_id: int | None = None
    planned_total_minutes: float = 0.0
    tasks_catalog: list[str] = Field(default_factory=list)
    sessions: list[PlanSession] = Field(default_factory=list)
    missing_data_notes: list[str] = Field(default_factory=list)
    summary_text: str = ""


class ExecutionLog(BaseModel):
    log_id: int
    plan_id: int | None = None
    patient_id: int | None = None
    therapist_id: int | None = None
    task_name: str | None = None
    device_id: int | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_seconds: float = 0.0
    duration_minutes: float = 0.0
    is_complete: int | None = None
    score: int | None = None
    task_type: int | None = None


class ExecutionSummary(BaseModel):
    time_range: TimeRange
    source_backend: str
    log_count: int = 0
    total_duration_minutes: float = 0.0
    unique_plan_ids: list[int] = Field(default_factory=list)
    logs: list[ExecutionLog] = Field(default_factory=list)
    log_count_by_plan: dict[int, int] = Field(default_factory=dict)
    duration_minutes_by_plan: dict[int, float] = Field(default_factory=dict)
    task_count_by_name: dict[str, int] = Field(default_factory=dict)
    missing_data_notes: list[str] = Field(default_factory=list)
    summary_text: str = ""


class OutcomeReport(BaseModel):
    report_id: int
    plan_id: int | None = None
    patient_id: int | None = None
    therapist_id: int | None = None
    create_time: datetime | None = None
    health_score: int | None = None
    game_score: int | None = None
    total_training_minutes: float = 0.0
    walk_distance: float = 0.0
    sit_count: int = 0
    balance_time: float = 0.0
    game_score_from_detail: float = 0.0
    detail_modes: list[str] = Field(default_factory=list)
    raw_metrics: dict[str, Any] = Field(default_factory=dict)
    highlight_text: str = ""


class OutcomeChangeSummary(BaseModel):
    time_range: TimeRange
    source_backend: str
    report_count: int = 0
    reports: list[OutcomeReport] = Field(default_factory=list)
    report_count_by_plan: dict[int, int] = Field(default_factory=dict)
    training_minutes_by_plan: dict[int, float] = Field(default_factory=dict)
    latest_training_minutes: float | None = None
    baseline_training_minutes: float | None = None
    training_minutes_delta: float | None = None
    latest_walk_distance: float | None = None
    baseline_walk_distance: float | None = None
    walk_distance_delta: float | None = None
    latest_game_score: float | None = None
    baseline_game_score: float | None = None
    game_score_delta: float | None = None
    trend_label: str = "unknown"
    missing_data_notes: list[str] = Field(default_factory=list)
    summary_text: str = ""


class GaitSessionExplanation(BaseModel):
    walk_plan_id: int
    item_id: int | None = None
    patient_id: int | None = None
    start_time: datetime | None = None
    duration_minutes: float | None = None
    completion_rate: float | None = None
    correct_rate: float | None = None
    error_rate: float | None = None
    avg_speed: float | None = None
    distance: float | None = None
    explanation: str = ""


class GaitExplanationSummary(BaseModel):
    patient_id: int | None = None
    time_range: TimeRange
    source_backend: str
    available: bool = False
    note: str = ""
    sessions: list[GaitSessionExplanation] = Field(default_factory=list)


class DeviationMetrics(BaseModel):
    scheduled_sessions: int = 0
    arrived_sessions: int = 0
    completed_sessions: int = 0
    attendance_rate: float = 0.0
    completion_rate: float = 0.0
    dose_adherence_rate: float = 0.0
    dose_deviation_rate: float = 0.0
    avg_planned_minutes: float = 0.0
    avg_actual_minutes: float = 0.0
    interruption_risk_score: float = 0.0
    consecutive_missed_sessions: int = 0
    risk_score: float = 0.0
    risk_level: RiskLevel = "low"
    driver_flags: list[str] = Field(default_factory=list)
    summary_text: str = ""


class ReflectionResult(BaseModel):
    evidence_sufficient: bool = True
    missing_fields: list[str] = Field(default_factory=list)
    consistency_notes: list[str] = Field(default_factory=list)
    recommend_manual_confirmation: bool = False
    manual_confirmation_reasons: list[str] = Field(default_factory=list)
    summary_text: str = ""


class ReviewCard(BaseModel):
    patient_id: int
    therapist_id: int | None = None
    primary_plan_id: int | None = None
    time_range: TimeRange
    source_backend: str
    plan_summary: PlanSummary
    execution_summary: ExecutionSummary
    deviation_metrics: DeviationMetrics
    outcome_change: OutcomeChangeSummary
    gait_explanation: GaitExplanationSummary
    review_focus: list[str] = Field(default_factory=list)
    initial_interventions: list[str] = Field(default_factory=list)
    reflection: ReflectionResult
    narrative_summary: str = ""


class PatientRiskSummary(BaseModel):
    patient_id: int
    therapist_id: int | None = None
    latest_plan_id: int | None = None
    risk_level: RiskLevel
    risk_score: float
    recent_attendance_rate: float
    recent_completion_rate: float
    recent_dose_adherence_rate: float
    interruption_risk_score: float
    outcome_trend: str
    review_priority: str
    summary: str


class WeeklyRiskReport(BaseModel):
    therapist_id: int
    time_range: TimeRange
    source_backend: str
    patient_count: int = 0
    high_risk_count: int = 0
    medium_risk_count: int = 0
    low_risk_count: int = 0
    deviation_statistics: dict[str, float] = Field(default_factory=dict)
    outcome_statistics: dict[str, float] = Field(default_factory=dict)
    patients: list[PatientRiskSummary] = Field(default_factory=list)
    priority_patient_ids: list[int] = Field(default_factory=list)
    summary_text: str = ""
    generated_at: datetime
