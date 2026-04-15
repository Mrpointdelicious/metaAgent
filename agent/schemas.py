from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from config import LLMProvider
from models import AnalyticsResultRow, PatientRiskSummary, PatientSet, LastVisitInfo, PlanStatus, RankedPatients
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


class OrchestratorRequest(BaseModel):
    task_type: TaskType | None = Field(default=None, description="来自命令行或对话层的预解析任务类型。")
    patient_id: int | None = Field(default=None, description="患者标识。")
    plan_id: int | None = Field(default=None, description="用于 A 链复核的计划标识。")
    therapist_id: int | None = Field(default=None, description="治疗师或医生标识。")
    days: int | None = Field(default=None, description="相对时间窗口天数。")
    top_k: int = Field(default=10, ge=1, le=100, description="群体任务返回的最大患者数量。")
    raw_text: str | None = Field(default=None, description="用户原始输入。")
    use_agent_sdk: bool | None = Field(default=None, description="执行模式覆盖。")
    llm_provider: LLMProvider | None = Field(default=None, description="运行时大模型厂商覆盖。")
    llm_model: str | None = Field(default=None, description="运行时大模型名称覆盖。")
    llm_base_url: str | None = Field(default=None, description="运行时模型接口基础地址覆盖。")
    need_outcome: bool | None = Field(default=None, description="是否需要包含结果层证据。")
    need_gait_evidence: bool | None = Field(default=None, description="是否需要包含 B 链步态证据。")
    response_style: str | None = Field(default=None, description="响应风格提示，例如标准或详细。")
    context: dict[str, Any] = Field(default_factory=dict, description="用于多轮续接的结构化上下文。")

    @property
    def normalized_task_type(self) -> OrchestrationTaskType:
        return normalize_task_type(self.task_type)


class OrchestrationIntent(BaseModel):
    raw_user_query: str = Field(default="", description="对话层预处理后的原始用户问题。")
    task_type: OrchestrationTaskType = Field(
        default=OrchestrationTaskType.UNKNOWN,
        description="路由后的结构化任务类型。",
    )
    plan_id: int | None = Field(default=None, description="A 链计划标识。")
    therapist_id: int | None = Field(default=None, description="治疗师或医生标识。")
    patient_id: int | None = Field(default=None, description="患者标识。")
    days: int | None = Field(default=None, description="请求的时间窗口天数。")
    top_k: int | None = Field(default=None, description="群体任务请求返回的数量上限。")
    need_outcome: bool = Field(default=True, description="是否需要结果层证据。")
    need_gait_evidence: bool = Field(
        default=False,
        description="是否需要以独立证据块形式返回 B 链步态证据。",
    )
    response_style: str = Field(default="standard", description="响应风格提示。")
    confidence: float | None = Field(default=None, description="路由阶段的置信度分数。")
    missing_slots: list[str] = Field(default_factory=list, description="完成任务仍缺失的关键槽位。")


AnalyticsIntentName = Literal[
    "single_patient_review",
    "risk_screening",
    "weekly_report",
    "open_analytics_query",
]


class IntentDecision(BaseModel):
    intent: AnalyticsIntentName = Field(description="路由器判定的顶层意图。")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="规则路由置信度。")
    rationale: str = Field(default="", description="路由原因说明。")


class QueryPlanStep(BaseModel):
    step_id: str = Field(description="查询计划步骤 ID。")
    intent: str = Field(description="该步骤的分析意图。")
    tool_name: str = Field(description="本步骤调用的 primitive tool 名称。")
    arguments: dict[str, Any] = Field(default_factory=dict, description="工具参数。")
    rationale: str = Field(default="", description="该步骤存在的原因。")


class QueryPlan(BaseModel):
    normalized_question: str = Field(default="", description="归一化后的分析问题。")
    doctor_id: int | None = Field(default=None, description="当前分析使用的医生 ID。")
    start_date: str | None = Field(default=None, description="分析窗口起始日期。")
    end_date: str | None = Field(default=None, description="分析窗口结束日期。")
    steps: list[QueryPlanStep] = Field(default_factory=list, description="顺序查询计划。")


class AnalyticsStructuredOutput(BaseModel):
    question: str = Field(default="", description="原始问题。")
    doctor_id: int | None = Field(default=None, description="本次分析对应的医生 ID。")
    time_range: TimeRange | None = Field(default=None, description="实际采用的分析时间窗。")
    source_backend: str = Field(default="", description="底层数据来源，例如 mysql 或 mock。")
    query_plan: QueryPlan = Field(description="实际执行的查询计划。")
    historical_seen_set: PatientSet | None = Field(default=None, description="历史到训患者集合。")
    recent_seen_set: PatientSet | None = Field(default=None, description="最近窗口到训患者集合。")
    absent_set: PatientSet | None = Field(default=None, description="以前来过但最近未到训的患者集合。")
    ranked_patients: RankedPatients | None = Field(default=None, description="排序结果。")
    result_rows: list[AnalyticsResultRow] = Field(default_factory=list, description="面向训练师的结果行。")
    evidence_basis: list[str] = Field(default_factory=list, description="本次分析引用的数据依据摘要。")
    summary: str = Field(default="", description="结果摘要。")


class PlanStep(BaseModel):
    step_id: str = Field(description="编排计划中的确定性步骤 ID。")
    tool_name: str = Field(description="允许执行的白名单工具名。")
    args: dict[str, Any] = Field(default_factory=dict, description="经过校验的工具参数。")
    reason: str = Field(description="该步骤存在的原因。")


class OrchestrationPlan(BaseModel):
    intent: OrchestrationIntent = Field(description="该计划所基于的意图对象。")
    mode: ExecutionMode = Field(description="编排器选择的执行模式。")
    constraints: list[str] = Field(default_factory=list, description="规划阶段必须遵守的护栏约束。")
    steps: list[PlanStep] = Field(default_factory=list, description="按顺序执行的计划步骤。")
    planner_notes: list[str] = Field(default_factory=list, description="不可执行的规划备注。")


class StepExecutionResult(BaseModel):
    step_id: str = Field(description="对应计划中的步骤 ID。")
    tool_name: str = Field(description="本步骤执行的工具名。")
    success: bool = Field(description="工具是否执行成功。")
    args: dict[str, Any] = Field(default_factory=dict, description="实际使用的工具参数。")
    output_summary: str = Field(default="", description="工具输出的紧凑摘要。")
    raw_output: Any | None = Field(default=None, description="用于调试或后续校验的原始输出。")
    error: str | None = Field(default=None, description="步骤失败时的错误信息。")


class OrchestrationState(BaseModel):
    user_query: str = Field(default="", description="原始用户问题。")
    intent: OrchestrationIntent | None = Field(default=None, description="当前路由得到的意图对象。")
    plan: OrchestrationPlan | None = Field(default=None, description="当前编排计划。")
    step_results: list[StepExecutionResult] = Field(default_factory=list, description="有序的执行轨迹。")
    structured_output: dict[str, Any] | None = Field(default=None, description="程序可消费的最终结构化输出。")
    final_text: str | None = Field(default=None, description="面向人的最终文本输出。")
    validation_issues: list[str] = Field(default_factory=list, description="校验阶段发现的问题。")
    mode: ExecutionMode = Field(default="direct", description="当前执行模式。")


class RiskScreenOutput(BaseModel):
    therapist_id: int = Field(description="被筛选患者群对应的治疗师 ID。")
    time_range: TimeRange | None = Field(default=None, description="当前覆盖的时间范围。")
    source_backend: str = Field(description="数据源类型，例如 mysql 或 mock。")
    patient_count: int = Field(default=0, description="底层患者群的总人数。")
    selected_count: int = Field(default=0, description="当前输出中返回的患者人数。")
    patients: list[PatientRiskSummary] = Field(default_factory=list, description="按风险排序后的患者摘要列表。")
    priority_patient_ids: list[int] = Field(default_factory=list, description="优先复核患者 ID 列表。")
    review_card_summaries: list[dict[str, Any]] = Field(
        default_factory=list,
        description="当响应风格要求更详细时，对前几名患者补充的复核摘要。",
    )
    summary_text: str = Field(default="", description="紧凑文本摘要。")


class OrchestratorResponse(BaseModel):
    success: bool = Field(description="本次编排是否整体成功。")
    task_type: str = Field(description="规范化后的任务类型字符串。")
    execution_mode: str = Field(default="direct", description="编排器实际使用的执行模式。")
    llm_provider: LLMProvider | None = Field(default=None, description="解析后的大模型厂商。")
    llm_model: str | None = Field(default=None, description="解析后的大模型名称。")
    structured_output: dict[str, Any] = Field(default_factory=dict, description="最终结构化输出。")
    final_text: str = Field(default="", description="可直接阅读的最终回复。")
    validation_issues: list[str] = Field(default_factory=list, description="当前运行中的校验发现项。")
    execution_trace: list[StepExecutionResult] = Field(default_factory=list, description="有序步骤执行轨迹。")
