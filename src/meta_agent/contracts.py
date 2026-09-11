"""
创建日期：2026-09-08
文件功能：实现冻结版意图、计划、事实、事件及原生运行状态契约。
"""

from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def fingerprint(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), default=str).encode()).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


Domain = Literal["conversation", "irego", "iremo", "iretour", "scene", "doctors",
                 "health", "product", "help", "hospital"]
GoalKind = Literal["chat", "rehab_overview", "rehab_history", "rehab_session",
                   "rehab_trend", "report", "scene_action", "doctor_query",
                   "knowledge_query", "unsupported"]
OutcomeStatus = Literal["succeeded", "partial", "unavailable", "failed", "cancelled",
                        "clarification", "unsupported", "blocked_dependency", "skipped_condition"]
Effect = Literal["read", "prepare_artifact", "emit_frontend_action"]
Capability = Literal["rehab.overview", "rehab.history", "rehab.resolve_session", "rehab.session",
                     "rehab.trend", "rehab.single_report", "rehab.trend_report", "scene.resolve",
                     "scene.dispatch", "doctors.search", "knowledge.search", "answer.compose"]


class Selector(StrictModel):
    mode: Literal["none", "current_ref", "latest_record", "latest_usable", "previous_record",
                  "ordinal", "date_range", "latest_count"] = "none"
    count: int | None = Field(default=None, ge=1, le=100)
    start: str | None = None
    end: str | None = None
    candidate_ref: str | None = None


class Condition(StrictModel):
    text: str = Field(min_length=1, max_length=500)
    source_goal_ids: list[str] = Field(min_length=1, max_length=6)


class Goal(StrictModel):
    goal_id: str = Field(min_length=1, max_length=160)
    kind: GoalKind
    domain: Domain
    query_span: str
    selector: Selector = Field(default_factory=Selector)
    topics: list[str] = Field(default_factory=list, max_length=10)
    output: Literal["answer", "list", "artifact", "action", "answer_and_artifact"] = "answer"
    excluded_outputs: list[Literal["artifact", "action"]] = Field(default_factory=list, max_length=2)
    after_goal_ids: list[str] = Field(default_factory=list, max_length=6)
    missing_slots: list[str] = Field(default_factory=list, max_length=8)
    clarification: str | None = None
    condition: Condition | None = None


class IntentDecision(StrictModel):
    contract_type: Literal["IntentDecision"] = "IntentDecision"
    schema_version: Literal["1.0"] = "1.0"
    decision: Literal["execute", "respond", "clarify", "unsupported"]
    goals: list[Goal] = Field(default_factory=list, max_length=6)
    decision_summary: str = Field(default="", max_length=500)


class Binding(StrictModel):
    argument: str
    source_kind: Literal["task_output", "evidence_index"]
    source_id: str
    field: Literal["session_ref", "window", "artifact_ref", "evidence_id", "space_id", "action_ref"]
    item_key: str | None = None


class Guard(StrictModel):
    source_task_id: str
    predicate: Literal["has_results", "no_results", "record_completed", "report_available",
                       "scene_context_confirmed"]
    on_false: Literal["skip", "clarify"] = "skip"


class TaskSpec(StrictModel):
    task_id: str
    goal_ids: list[str] = Field(min_length=1, max_length=6)
    capability: Capability
    arguments: dict[str, Any] = Field(default_factory=dict)
    bindings: list[Binding] = Field(default_factory=list, max_length=8)
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    guards: list[Guard] = Field(default_factory=list, max_length=6)
    effect: Effect = "read"
    priority_class: Literal["action", "interactive", "artifact"] = "interactive"
    timeout_ms: int = Field(default=15000, gt=0, le=40000)
    retry_limit: int = Field(default=1, ge=0, le=1)
    idempotency_key: str


class ValidatedPlan(StrictModel):
    contract_type: Literal["ValidatedPlan"] = "ValidatedPlan"
    schema_version: Literal["1.0"] = "1.0"
    request_id: str
    plan_revision: int = Field(default=1, ge=1, le=2)
    goal_ids: list[str] = Field(default_factory=list, max_length=6)
    tasks: list[TaskSpec] = Field(default_factory=list, max_length=8)
    outcome_hint: Literal["execute", "respond", "clarify", "partial", "unsupported"] = "execute"
    deadline_ms: int = Field(default=60000, gt=0, le=60000)


class Fact(StrictModel):
    contract_type: Literal["Fact"] = "Fact"
    schema_version: Literal["1.0"] = "1.0"
    fact_id: str
    evidence_id: str
    path: str = Field(pattern=r"^/")
    semantic_key: str
    value: str | int | float | bool | None
    unit: str | None = None
    value_status: Literal["valid", "missing", "invalid", "unknown", "not_comparable"] = "valid"
    observed_at: datetime | None = None
    record_ref: str | None = None
    source_version: str
    source_type: Literal["tool", "user_statement", "document", "derived"] = "tool"
    allowed_uses: list[Literal["display", "single_session", "trend", "knowledge_explanation"]] = (
        Field(default_factory=lambda: ["display"])
    )
    audience: Literal["patient", "clinician", "both"] = "both"


class FactView(StrictModel):
    """显示信息来自适配器，既不改写事实值也不成为新的事实来源。"""
    fact: Fact
    label: str
    topic: str = "general"
    required: bool = False


EventType = Literal["accepted", "progress", "action_ready", "answer_part", "artifact_ready",
                    "clarification", "task_failed", "completed"]


class OutboundEvent(StrictModel):
    contract_type: Literal["OutboundEvent"] = "OutboundEvent"
    schema_version: Literal["1.0"] = "1.0"
    event_id: str = Field(default_factory=lambda: identifier("event"))
    request_id: str
    run_id: str
    conversation_id: str
    seq: int = Field(ge=1)
    task_id: str | None = None
    goal_id: str | None = None
    type: EventType
    payload: dict[str, Any]
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_payload(self) -> "OutboundEvent":
        payload_types[self.type].model_validate(self.payload)
        if self.type == "completed" and self.payload["event_range"]["last_seq"] != self.seq:
            raise ValueError("completed event_range must include its own sequence")
        return self


class TextPayload(StrictModel):
    text: str


class ProgressPayload(TextPayload):
    stage: str


class AnswerPayload(TextPayload):
    fact_ids: list[str] = Field(default_factory=list, max_length=100)
    doc_refs: list[str] = Field(default_factory=list, max_length=20)
    revision: int = Field(default=1, ge=1)
    replaces: str | None = None


class ActionPayload(StrictModel):
    action_id: str
    command_code: str
    profile: Literal["native", "dify-tagged", "legacy-direct"] = "native"
    delivery_status: Literal["ready", "dispatched", "delivery_unknown"] = "ready"
    source_evidence_id: str
    source_space_id: str
    scene_version: int = Field(ge=0)
    command_order: int = Field(ge=1)


class ArtifactPayload(StrictModel):
    artifact_ref: str
    url: str
    expires_at: datetime | None = None
    evidence_id: str


class ClarificationPayload(TextPayload):
    missing_slots: list[str] = Field(default_factory=list)


class FailurePayload(TextPayload):
    code: str
    retryable: bool = False


class EventRange(StrictModel):
    first_seq: int = Field(ge=1)
    last_seq: int = Field(ge=1)


class CompletionPayload(StrictModel):
    outcome: OutcomeStatus
    goal_statuses: dict[str, OutcomeStatus]
    task_statuses: dict[str, OutcomeStatus]
    event_range: EventRange


payload_types: dict[str, type[StrictModel]] = {
    "accepted": TextPayload, "progress": ProgressPayload, "answer_part": AnswerPayload,
    "action_ready": ActionPayload, "artifact_ready": ArtifactPayload,
    "clarification": ClarificationPayload, "task_failed": FailurePayload,
    "completed": CompletionPayload,
}


class TaskResult(StrictModel):
    task_id: str
    status: OutcomeStatus = "succeeded"
    message: str = ""
    code: str = ""
    retryable: bool = False
    evidence_ids: list[str] = Field(default_factory=list)
    facts: list[FactView] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 1
    elapsed_ms: float = 0


class RecordAnchor(StrictModel):
    session_ref: str
    evidence_id: str
    observed_at: datetime = Field(default_factory=utcnow)
    session_time: str | None = None
    source_version: str = "unknown"


class ConversationState(StrictModel):
    turns: list[dict[str, str]] = Field(default_factory=list)
    current_record: RecordAnchor | None = None
    history_page: int = 1
    pending_goals: list[Goal] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utcnow)


class RunRecord(StrictModel):
    run_id: str
    request_id: str
    request_hash: str
    scope_key: str
    conversation_id: str
    status: str = "running"
    created_at: datetime = Field(default_factory=utcnow)
    events: list[OutboundEvent] = Field(default_factory=list)
    decision: IntentDecision | None = None
    plan: ValidatedPlan | None = None
    results: dict[str, TaskResult] = Field(default_factory=dict)
    goal_statuses: dict[str, OutcomeStatus] = Field(default_factory=dict)
    action_delivery: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)


class DomainError(RuntimeError):
    """异常只携带稳定错误分类与可展示说明，不附带HTTP正文或身份。"""
    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 outcome: OutcomeStatus = "failed") -> None:
        super().__init__(message)
        self.code, self.message = code, message
        self.retryable, self.outcome = retryable, outcome
