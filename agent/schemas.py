from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from config import LLMProvider
from models import (
    AnalyticsResultRow,
    DoctorAnalyticsResultRow,
    LastVisitInfo,
    PatientRiskSummary,
    PatientSet,
    PlanStatus,
    RankedPatients,
)
from models.common import TimeRange


class OrchestrationTaskType(str, Enum):
    REVIEW_PATIENT = "review_patient"
    SCREEN_RISK = "screen_risk"
    WEEKLY_REPORT = "weekly_report"
    GAIT_REVIEW = "gait_review"
    OPEN_ANALYTICS_QUERY = "open_analytics_query"
    UNKNOWN = "unknown"


LegacyTaskType = Literal["single_review", "risk_screen", "weekly_report", "unsupported"]
TaskType = Literal[
    "review_patient",
    "screen_risk",
    "weekly_report",
    "gait_review",
    "open_analytics_query",
    "unknown",
    "single_review",
    "risk_screen",
    "unsupported",
]
ExecutionMode = Literal["direct", "agents_sdk"]

OpenAnalyticsSubtype = Literal[
    "absent_old_patients_recent_window",
    "absent_from_baseline_window",
    "doctors_with_active_plans",
]
AnalyticsScope = Literal["single_doctor", "doctor_aggregate", "patient_single"]
AnalyticsParseMode = Literal["single_window", "dual_window", "fallback"]
DoctorIdSource = Literal["explicit", "session", "none"]

AnalyticsIntentName = Literal[
    "single_patient_review",
    "risk_screening",
    "weekly_report",
    "open_analytics_query",
]


_LEGACY_TO_NORMALIZED: dict[str, OrchestrationTaskType] = {
    "single_review": OrchestrationTaskType.REVIEW_PATIENT,
    "risk_screen": OrchestrationTaskType.SCREEN_RISK,
    "weekly_report": OrchestrationTaskType.WEEKLY_REPORT,
    "unsupported": OrchestrationTaskType.UNKNOWN,
}
_NORMALIZED_TO_LEGACY: dict[OrchestrationTaskType, str] = {
    OrchestrationTaskType.REVIEW_PATIENT: "single_review",
    OrchestrationTaskType.SCREEN_RISK: "risk_screen",
    OrchestrationTaskType.WEEKLY_REPORT: "weekly_report",
    OrchestrationTaskType.GAIT_REVIEW: "unsupported",
    OrchestrationTaskType.OPEN_ANALYTICS_QUERY: "unsupported",
    OrchestrationTaskType.UNKNOWN: "unsupported",
}


def normalize_task_type(value: TaskType | OrchestrationTaskType | None) -> OrchestrationTaskType:
    if value is None:
        return OrchestrationTaskType.UNKNOWN
    if isinstance(value, OrchestrationTaskType):
        return value
    if value in _LEGACY_TO_NORMALIZED:
        return _LEGACY_TO_NORMALIZED[value]
    try:
        return OrchestrationTaskType(value)
    except ValueError:
        return OrchestrationTaskType.UNKNOWN


def legacy_task_type(value: TaskType | OrchestrationTaskType | None) -> str:
    normalized = normalize_task_type(value)
    return _NORMALIZED_TO_LEGACY[normalized]


class RelativeWindow(BaseModel):
    start_offset_days: int | None = Field(default=None, description="Start offset from anchor, in days.")
    end_offset_days: int | None = Field(default=None, description="End offset from anchor, in days.")
    label: str | None = Field(default=None, description="Human-readable label for this relative window.")


class AnalyticsTimeSlots(BaseModel):
    anchor_strategy: str = Field(default="plan_anchor_or_now", description="Anchor resolution strategy.")
    recent_window: RelativeWindow | None = Field(default=None, description="Primary recent analysis window.")
    baseline_window: RelativeWindow | None = Field(default=None, description="Optional baseline comparison window.")
    raw_days: int | None = Field(default=None, description="Compatibility days field used by legacy callers.")
    parse_mode: AnalyticsParseMode = Field(default="fallback", description="How the time slots were parsed.")
    note: str | None = Field(default=None, description="Optional parser note.")


class ResolvedWindow(BaseModel):
    start: str = Field(description="Resolved start datetime.")
    end: str = Field(description="Resolved end datetime.")
    label: str = Field(description="Resolved date label.")


class ResolvedAnalyticsRanges(BaseModel):
    anchor_time: str = Field(description="Anchor datetime used to resolve relative windows.")
    recent_window: ResolvedWindow | None = Field(default=None, description="Resolved recent window.")
    baseline_window: ResolvedWindow | None = Field(default=None, description="Resolved baseline window.")


class OrchestratorRequest(BaseModel):
    task_type: TaskType | None = Field(default=None, description="Task type from CLI or dialogue layer.")
    patient_id: int | None = Field(default=None, description="Patient identifier.")
    plan_id: int | None = Field(default=None, description="Plan identifier.")
    therapist_id: int | None = Field(default=None, description="Therapist or doctor identifier.")
    days: int | None = Field(default=None, description="Legacy relative time window in days.")
    analytics_time_slots: AnalyticsTimeSlots | None = Field(
        default=None,
        description="Optional structured analytics time slots.",
    )
    top_k: int = Field(default=10, ge=1, le=100, description="Max result size for grouped tasks.")
    raw_text: str | None = Field(default=None, description="Raw user input.")
    use_agent_sdk: bool | None = Field(default=None, description="Execution mode override.")
    llm_provider: LLMProvider | None = Field(default=None, description="Runtime LLM provider override.")
    llm_model: str | None = Field(default=None, description="Runtime LLM model override.")
    llm_base_url: str | None = Field(default=None, description="Runtime LLM base URL override.")
    need_outcome: bool | None = Field(default=None, description="Whether outcome evidence is required.")
    need_gait_evidence: bool | None = Field(default=None, description="Whether gait evidence is required.")
    response_style: str | None = Field(default=None, description="Response style hint.")
    context: dict[str, Any] = Field(default_factory=dict, description="Structured conversation context.")

    @property
    def normalized_task_type(self) -> OrchestrationTaskType:
        return normalize_task_type(self.task_type)


class OrchestrationIntent(BaseModel):
    raw_user_query: str = Field(default="", description="Normalized raw user query.")
    task_type: OrchestrationTaskType = Field(
        default=OrchestrationTaskType.UNKNOWN,
        description="Structured task type after routing.",
    )
    plan_id: int | None = Field(default=None, description="Plan identifier.")
    therapist_id: int | None = Field(default=None, description="Therapist or doctor identifier.")
    patient_id: int | None = Field(default=None, description="Patient identifier.")
    days: int | None = Field(default=None, description="Requested time window in days.")
    top_k: int | None = Field(default=None, description="Requested result size limit.")
    need_outcome: bool = Field(default=True, description="Whether outcome evidence is required.")
    need_gait_evidence: bool = Field(default=False, description="Whether gait evidence is required.")
    response_style: str = Field(default="standard", description="Response style hint.")
    confidence: float | None = Field(default=None, description="Routing confidence.")
    missing_slots: list[str] = Field(default_factory=list, description="Missing slots required to execute.")


class IntentDecision(BaseModel):
    intent: AnalyticsIntentName = Field(description="Top-level routed intent.")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Rule-based routing confidence.")
    rationale: str = Field(default="", description="Why the router chose this intent.")
    analytics_subtype: OpenAnalyticsSubtype | None = Field(default=None, description="Open analytics subtype.")
    analysis_scope: AnalyticsScope | None = Field(default=None, description="Open analytics scope.")
    doctor_id_source: DoctorIdSource | None = Field(default=None, description="Rule-derived doctor ID source.")


class LLMRouteDecision(BaseModel):
    intent: AnalyticsIntentName = Field(description="LLM-refined top-level intent.")
    analytics_subtype: OpenAnalyticsSubtype | None = Field(default=None, description="LLM-refined open analytics subtype.")
    scope: AnalyticsScope | None = Field(default=None, description="LLM-refined task scope.")
    doctor_id_source: DoctorIdSource | None = Field(default=None, description="Source used for doctor ID resolution.")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="LLM routing confidence.")
    rationale: str = Field(default="", description="Why the LLM chose this route.")


class RoutedDecision(BaseModel):
    rule_decision: IntentDecision = Field(description="Deterministic rule router decision.")
    llm_decision: LLMRouteDecision | None = Field(default=None, description="Optional LLM refinement decision.")
    final_intent: AnalyticsIntentName = Field(description="Merged final intent.")
    final_subtype: OpenAnalyticsSubtype | None = Field(default=None, description="Merged final open analytics subtype.")
    final_scope: AnalyticsScope | None = Field(default=None, description="Merged final scope.")
    doctor_id_source: DoctorIdSource | None = Field(default=None, description="Merged doctor ID source.")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Merged confidence.")
    rationale: str = Field(default="", description="Merged routing rationale.")


class QueryPlanStep(BaseModel):
    step_id: str = Field(description="Query plan step ID.")
    intent: str = Field(description="Intent served by this step.")
    tool_name: str = Field(description="Primitive tool called by this step.")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Tool arguments.")
    rationale: str = Field(default="", description="Why this step exists.")


class QueryPlan(BaseModel):
    normalized_question: str = Field(default="", description="Normalized analysis question.")
    subtype: OpenAnalyticsSubtype | None = Field(default=None, description="Open analytics subtype.")
    analysis_scope: AnalyticsScope | None = Field(default=None, description="Analysis scope.")
    doctor_id: int | None = Field(default=None, description="Doctor filter used by the plan.")
    start_date: str | None = Field(default=None, description="Legacy start date, mapped to recent window start.")
    end_date: str | None = Field(default=None, description="Legacy end date, mapped to recent window end.")
    recent_start_date: str | None = Field(default=None, description="Recent window start date.")
    recent_end_date: str | None = Field(default=None, description="Recent window end date.")
    baseline_start_date: str | None = Field(default=None, description="Baseline window start date.")
    baseline_end_date: str | None = Field(default=None, description="Baseline window end date.")
    time_parse_mode: AnalyticsParseMode | None = Field(default=None, description="Time parsing mode.")
    time_slots: AnalyticsTimeSlots | None = Field(default=None, description="Parsed relative time slots.")
    resolved_ranges: ResolvedAnalyticsRanges | None = Field(default=None, description="Resolved actual windows.")
    steps: list[QueryPlanStep] = Field(default_factory=list, description="Ordered query plan steps.")


class AnalyticsStructuredOutput(BaseModel):
    question: str = Field(default="", description="Original question.")
    subtype: OpenAnalyticsSubtype | None = Field(default=None, description="Open analytics subtype.")
    analysis_scope: AnalyticsScope | None = Field(default=None, description="Analysis scope.")
    doctor_id: int | None = Field(default=None, description="Doctor filter used for this analysis.")
    time_range: TimeRange | None = Field(default=None, description="Legacy recent time range.")
    time_slots: AnalyticsTimeSlots | None = Field(default=None, description="Structured time slots.")
    resolved_ranges: ResolvedAnalyticsRanges | None = Field(default=None, description="Resolved actual ranges.")
    source_backend: str = Field(default="", description="Underlying data backend.")
    query_plan: QueryPlan = Field(description="Executed query plan.")
    historical_seen_set: PatientSet | None = Field(default=None, description="Historical attendance cohort.")
    recent_seen_set: PatientSet | None = Field(default=None, description="Recent attendance cohort.")
    absent_set: PatientSet | None = Field(default=None, description="Patients absent in the target window.")
    ranked_patients: RankedPatients | None = Field(default=None, description="Ranked patient list for patient analyses.")
    result_rows: list[AnalyticsResultRow | DoctorAnalyticsResultRow] = Field(
        default_factory=list,
        description="Result rows for patient or doctor aggregate analyses.",
    )
    evidence_basis: list[str] = Field(default_factory=list, description="Evidence basis summary.")
    supported_subtypes: list[OpenAnalyticsSubtype] = Field(default_factory=list, description="Supported subtypes.")
    summary: str = Field(default="", description="Short analysis summary.")


class PlanStep(BaseModel):
    step_id: str = Field(description="Planner step ID.")
    tool_name: str = Field(description="Allowed tool name.")
    args: dict[str, Any] = Field(default_factory=dict, description="Validated tool arguments.")
    reason: str = Field(description="Why this step exists.")


class OrchestrationPlan(BaseModel):
    intent: OrchestrationIntent = Field(description="Intent driving the plan.")
    mode: ExecutionMode = Field(description="Execution mode selected by the orchestrator.")
    constraints: list[str] = Field(default_factory=list, description="Constraints the planner must obey.")
    steps: list[PlanStep] = Field(default_factory=list, description="Ordered execution steps.")
    planner_notes: list[str] = Field(default_factory=list, description="Planner notes.")


class StepExecutionResult(BaseModel):
    step_id: str = Field(description="Step ID from the plan.")
    tool_name: str = Field(description="Tool invoked for the step.")
    success: bool = Field(description="Whether the step succeeded.")
    args: dict[str, Any] = Field(default_factory=dict, description="Actual tool arguments.")
    output_summary: str = Field(default="", description="Compact output summary.")
    raw_output: Any | None = Field(default=None, description="Raw output for debugging or validation.")
    error: str | None = Field(default=None, description="Error string when the step fails.")


class OrchestrationState(BaseModel):
    user_query: str = Field(default="", description="Original user query.")
    intent: OrchestrationIntent | None = Field(default=None, description="Current routed intent.")
    plan: OrchestrationPlan | None = Field(default=None, description="Current orchestration plan.")
    step_results: list[StepExecutionResult] = Field(default_factory=list, description="Ordered execution trace.")
    structured_output: dict[str, Any] | None = Field(default=None, description="Program-consumable output.")
    final_text: str | None = Field(default=None, description="Human-facing final text.")
    validation_issues: list[str] = Field(default_factory=list, description="Validation issues.")
    mode: ExecutionMode = Field(default="direct", description="Current execution mode.")


class RiskScreenOutput(BaseModel):
    therapist_id: int = Field(description="Therapist identifier for the screened cohort.")
    time_range: TimeRange | None = Field(default=None, description="Covered time range.")
    source_backend: str = Field(description="Data backend type.")
    patient_count: int = Field(default=0, description="Total cohort size.")
    selected_count: int = Field(default=0, description="Number of patients returned.")
    patients: list[PatientRiskSummary] = Field(default_factory=list, description="Risk-sorted patient summaries.")
    priority_patient_ids: list[int] = Field(default_factory=list, description="Priority patient IDs.")
    review_card_summaries: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Optional review card summaries for top patients.",
    )
    summary_text: str = Field(default="", description="Compact summary text.")


class OrchestratorResponse(BaseModel):
    success: bool = Field(description="Whether the orchestration succeeded.")
    task_type: str = Field(description="Normalized task type string.")
    execution_mode: str = Field(default="direct", description="Execution mode actually used.")
    llm_provider: LLMProvider | None = Field(default=None, description="Resolved LLM provider.")
    llm_model: str | None = Field(default=None, description="Resolved LLM model.")
    structured_output: dict[str, Any] = Field(default_factory=dict, description="Final structured output.")
    final_text: str = Field(default="", description="Human-readable final text.")
    validation_issues: list[str] = Field(default_factory=list, description="Validation issues found during execution.")
    execution_trace: list[StepExecutionResult] = Field(default_factory=list, description="Ordered execution trace.")
