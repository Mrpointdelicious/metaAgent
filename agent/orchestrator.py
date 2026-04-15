from __future__ import annotations

import re
from typing import Any

from config import ResolvedLLMConfig, Settings, get_settings
from models import GaitExplanationSummary, ReviewCard, WeeklyRiskReport
from repositories import RehabRepository
from services import (
    AnalyticsService,
    DeviationService,
    ExecutionService,
    GaitService,
    OutcomeService,
    PlanService,
    ReflectionService,
    ReportService,
)
from tools import (
    ToolSpec,
    build_analytics_tools,
    build_execution_tools,
    build_gait_tools,
    build_outcome_tools,
    build_plan_tools,
    build_reflection_tools,
    build_report_tools,
    build_tool_registry,
)
from .analytics_manager import AnalyticsManager
from .intent_router import IntentRouter

from .schemas import (
    ExecutionMode,
    OrchestrationIntent,
    OrchestrationPlan,
    OrchestrationState,
    OrchestrationTaskType,
    OrchestratorRequest,
    OrchestratorResponse,
    PlanStep,
    RiskScreenOutput,
    StepExecutionResult,
    normalize_task_type,
)


WEEKLY_KEYWORDS = ("周报", "weekly", "summary", "摘要")
SCREEN_KEYWORDS = ("高风险", "风险筛选", "优先复核", "risk", "screen")
REVIEW_KEYWORDS = ("复核", "计划", "患者", "病人", "review", "plan", "patient")
GAIT_KEYWORDS = ("步态", "步道", "gait", "walkway", "walk")
DETAIL_KEYWORDS = ("详细", "原因", "detail", "detailed", "reason", "why")
BRIEF_KEYWORDS = ("简短", "简洁", "brief", "short")
FOLLOW_UP_KEYWORDS = ("换成", "改成", "调整", "继续", "this", "same", "switch", "change")
UNRELIABLE_PHRASES = ("根据数据库推测", "大概", "猜测", "可能是数据库显示")


class RehabAgentOrchestrator:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.intent_router = IntentRouter()

        self.repository = RehabRepository(self.settings)
        self.analytics_service = AnalyticsService(self.repository, self.settings)
        self.plan_service = PlanService(self.repository, self.settings)
        self.execution_service = ExecutionService(self.repository, self.settings)
        self.outcome_service = OutcomeService(self.repository, self.settings)
        self.gait_service = GaitService(self.repository, self.settings)
        self.deviation_service = DeviationService(self.settings)
        self.reflection_service = ReflectionService()
        self.report_service = ReportService(
            self.repository,
            self.plan_service,
            self.execution_service,
            self.outcome_service,
            self.gait_service,
            self.deviation_service,
            self.reflection_service,
        )

        self.plan_tools = build_plan_tools(self.plan_service)
        self.execution_tools = build_execution_tools(
            self.plan_service,
            self.execution_service,
            self.outcome_service,
            self.deviation_service,
        )
        self.outcome_tools = build_outcome_tools(self.plan_service, self.outcome_service)
        self.gait_tools = build_gait_tools(self.gait_service)
        self.report_tools = build_report_tools(self.report_service)
        self.reflection_tools = build_reflection_tools(self.report_service)
        self.analytics_tools = build_analytics_tools(self.analytics_service)
        self.tool_registry = build_tool_registry(
            self.analytics_tools,
            self.plan_tools,
            self.execution_tools,
            self.outcome_tools,
            self.gait_tools,
            self.report_tools,
            self.reflection_tools,
        )
        self.analytics_manager = AnalyticsManager(
            analytics_service=self.analytics_service,
            analytics_tool_registry=build_tool_registry(self.analytics_tools),
            settings=self.settings,
        )

    def run(self, request: OrchestratorRequest) -> OrchestratorResponse:
        llm_config = self.settings.resolve_llm_config(
            provider=request.llm_provider,
            model=request.llm_model,
            base_url=request.llm_base_url,
        )
        mode, execution_mode, mode_issues = self._resolve_mode(request, llm_config)
        decision = self.intent_router.route(request)
        if decision.intent == "open_analytics_query":
            response = self.analytics_manager.run(
                request,
                decision,
                mode=mode,
                llm_config=llm_config,
                execution_mode=execution_mode,
            )
            response.validation_issues = list(mode_issues) + list(response.validation_issues)
            return response

        direct_request = request.model_copy(deep=True)
        if decision.intent == "single_patient_review" and direct_request.task_type is None:
            direct_request.task_type = OrchestrationTaskType.REVIEW_PATIENT.value
        elif decision.intent == "risk_screening" and direct_request.task_type is None:
            direct_request.task_type = OrchestrationTaskType.SCREEN_RISK.value
        elif decision.intent == "weekly_report" and direct_request.task_type is None:
            direct_request.task_type = OrchestrationTaskType.WEEKLY_REPORT.value
        state = OrchestrationState(
            user_query=direct_request.raw_text or "",
            mode=mode,
            validation_issues=list(mode_issues),
        )

        intent = self.route_intent(direct_request)
        state.intent = intent
        if intent.task_type == OrchestrationTaskType.UNKNOWN:
            state.final_text = self._render_unsupported(intent)
            state.validation_issues.extend([f"router.missing_slot:{slot}" for slot in intent.missing_slots])
            return self._build_response(state, llm_config, execution_mode, success=False)

        state.plan = self.build_plan(intent, mode)
        self.execute_plan(state)
        state.final_text = self.render_final_text(state)
        state.validation_issues.extend(self.validate_output(state))
        success = bool(state.structured_output) and not any(
            issue.startswith("structured_output.") or issue.startswith("schema.")
            for issue in state.validation_issues
        )
        return self._build_response(state, llm_config, execution_mode, success=success)

    def route_intent(self, request: OrchestratorRequest) -> OrchestrationIntent:
        raw_query = (request.raw_text or "").strip()
        context = dict(request.context or {})
        normalized_task = normalize_task_type(request.task_type)
        extracted = self._extract_slots(raw_query)

        if normalized_task == OrchestrationTaskType.UNKNOWN:
            normalized_task = self._infer_task_type(raw_query, context)

        patient_id = self._coalesce(request.patient_id, extracted.get("patient_id"), context.get("patient_id"))
        plan_id = self._coalesce(request.plan_id, extracted.get("plan_id"), context.get("plan_id"))
        therapist_id = self._coalesce(
            request.therapist_id,
            extracted.get("therapist_id"),
            context.get("therapist_id"),
        )
        days = self._coalesce(request.days, extracted.get("days"), context.get("days"))
        top_k = self._coalesce(request.top_k, extracted.get("top_k"), context.get("top_k"), 10)

        if normalized_task in {OrchestrationTaskType.SCREEN_RISK, OrchestrationTaskType.WEEKLY_REPORT}:
            therapist_id = therapist_id or self.settings.demo_default_therapist_id
            days = days or self.settings.default_weekly_report_days
        elif normalized_task == OrchestrationTaskType.REVIEW_PATIENT:
            days = days or self.settings.default_time_window_days
        elif normalized_task == OrchestrationTaskType.GAIT_REVIEW:
            days = days or self.settings.default_time_window_days

        response_style = request.response_style or extracted.get("response_style") or context.get("response_style") or "standard"
        need_outcome = request.need_outcome if request.need_outcome is not None else normalized_task != OrchestrationTaskType.GAIT_REVIEW
        requested_gait = request.need_gait_evidence
        if requested_gait is None:
            requested_gait = bool(extracted.get("need_gait_evidence") or context.get("need_gait_evidence"))
        if normalized_task in {OrchestrationTaskType.SCREEN_RISK, OrchestrationTaskType.WEEKLY_REPORT}:
            need_gait_evidence = False
        elif normalized_task == OrchestrationTaskType.GAIT_REVIEW:
            need_gait_evidence = True
        else:
            need_gait_evidence = bool(requested_gait)

        missing_slots: list[str] = []
        if normalized_task == OrchestrationTaskType.REVIEW_PATIENT and patient_id is None and plan_id is None:
            missing_slots.append("patient_id_or_plan_id")
        if normalized_task in {OrchestrationTaskType.SCREEN_RISK, OrchestrationTaskType.WEEKLY_REPORT} and therapist_id is None:
            missing_slots.append("therapist_id")
        if normalized_task == OrchestrationTaskType.GAIT_REVIEW and patient_id is None:
            missing_slots.append("patient_id")
        if normalized_task == OrchestrationTaskType.UNKNOWN:
            missing_slots.append("task_type")

        confidence = 0.95 if request.task_type else 0.75 if raw_query else 0.5
        if self._is_follow_up(raw_query):
            confidence = min(confidence + 0.05, 0.99)

        return OrchestrationIntent(
            raw_user_query=raw_query or "结构化请求",
            task_type=normalized_task,
            plan_id=plan_id,
            therapist_id=therapist_id,
            patient_id=patient_id,
            days=days,
            top_k=top_k,
            need_outcome=need_outcome,
            need_gait_evidence=need_gait_evidence,
            response_style=response_style,
            confidence=confidence,
            missing_slots=missing_slots,
        )

    def build_plan(self, intent: OrchestrationIntent, mode: ExecutionMode) -> OrchestrationPlan:
        constraints = self._build_default_constraints()
        planner_notes: list[str] = []
        steps: list[PlanStep] = []

        if intent.task_type == OrchestrationTaskType.REVIEW_PATIENT:
            steps.append(
                PlanStep(
                    step_id="step_1",
                    tool_name="generate_review_card",
                    args={
                        "patient_id": intent.patient_id,
                        "plan_id": intent.plan_id,
                        "therapist_id": intent.therapist_id,
                        "days": intent.days or self.settings.default_time_window_days,
                    },
                    reason="优先使用高层复核卡工具，确保核心业务逻辑仍由 `services/report_service` 维护。",
                )
            )
            if intent.need_gait_evidence:
                planner_notes.append(
                    "B 链步态证据在复核输出中只保留为独立证据块，不影响风险评分。"
                )
            steps.append(
                PlanStep(
                    step_id="step_2",
                    tool_name="reflect_on_output",
                    args={"task_type": intent.task_type.value},
                    reason="在生成复核卡后执行一次受约束护栏检查。",
                )
            )

        elif intent.task_type == OrchestrationTaskType.SCREEN_RISK:
            steps.append(
                PlanStep(
                    step_id="step_1",
                    tool_name="screen_risk_patients",
                    args={
                        "therapist_id": intent.therapist_id,
                        "days": intent.days or self.settings.default_weekly_report_days,
                        "top_k": intent.top_k or 10,
                    },
                    reason="优先使用高层治疗师筛选工具，而不是在规划阶段自行拼装风险逻辑。",
                )
            )
            if intent.response_style in {"detailed", "detail", "reasoned", "explain"}:
                steps.append(
                    PlanStep(
                        step_id="step_2",
                        tool_name="generate_review_card",
                        args={
                            "therapist_id": intent.therapist_id,
                            "days": intent.days or self.settings.default_weekly_report_days,
                            "_patient_ids_from": "step_1",
                            "_top_n": min(intent.top_k or 10, 3),
                        },
                        reason="当用户明确要求更多细节时，为筛选结果前几名患者补充复核卡摘要。",
                    )
                )
            if intent.need_gait_evidence:
                planner_notes.append(
                    "多患者风险筛选中忽略步态证据请求，因为 B 链证据不能影响 A 链群体风险逻辑。"
                )
            steps.append(
                PlanStep(
                    step_id=f"step_{len(steps) + 1}",
                    tool_name="reflect_on_output",
                    args={"task_type": intent.task_type.value},
                    reason="对聚合后的筛选输出执行受约束护栏检查。",
                )
            )

        elif intent.task_type == OrchestrationTaskType.WEEKLY_REPORT:
            steps.append(
                PlanStep(
                    step_id="step_1",
                    tool_name="generate_weekly_risk_report",
                    args={
                        "therapist_id": intent.therapist_id,
                        "days": intent.days or self.settings.default_weekly_report_days,
                        "top_k": intent.top_k or 10,
                    },
                    reason="通过 service 层聚合逻辑生成 A 链周报。",
                )
            )
            if intent.need_gait_evidence:
                planner_notes.append(
                    "周报任务中忽略步态证据请求，因为 B 链证据不会并入 A 链周报统计。"
                )
            steps.append(
                PlanStep(
                    step_id="step_2",
                    tool_name="reflect_on_output",
                    args={"task_type": intent.task_type.value},
                    reason="对周报输出执行受约束护栏检查。",
                )
            )

        elif intent.task_type == OrchestrationTaskType.GAIT_REVIEW:
            steps.append(
                PlanStep(
                    step_id="step_1",
                    tool_name="get_gait_explanation",
                    args={
                        "patient_id": intent.patient_id,
                        "days": intent.days or self.settings.default_time_window_days,
                    },
                    reason="调用专用的 B 链步态证据工具，同时不影响 A 链风险评分。",
                )
            )
            steps.append(
                PlanStep(
                    step_id="step_2",
                    tool_name="reflect_on_output",
                    args={"task_type": intent.task_type.value},
                    reason="对步态证据块执行受约束护栏检查。",
                )
            )

        return OrchestrationPlan(
            intent=intent,
            mode=mode,
            constraints=constraints,
            steps=steps,
            planner_notes=planner_notes,
        )

    def execute_plan(self, state: OrchestrationState) -> None:
        if state.plan is None:
            state.validation_issues.append("plan.missing")
            return

        for step in state.plan.steps:
            resolved_args = self._resolve_step_args(step, state)
            try:
                raw_output = self._call_tool(step.tool_name, resolved_args, state.mode)
                summary = self._summarize_tool_output(step.tool_name, raw_output)
                result = StepExecutionResult(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    success=True,
                    args=resolved_args,
                    output_summary=summary,
                    raw_output=raw_output,
                )
                self._merge_step_output(state, step, raw_output)
            except Exception as exc:  # noqa: BLE001
                result = StepExecutionResult(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    success=False,
                    args=resolved_args,
                    output_summary="步骤执行失败",
                    raw_output=None,
                    error=str(exc),
                )
                state.validation_issues.append(f"step_failed:{step.step_id}:{step.tool_name}")
            state.step_results.append(result)

    def validate_output(self, state: OrchestrationState) -> list[str]:
        issues: list[str] = []
        payload = state.structured_output
        task_type = state.intent.task_type if state.intent else OrchestrationTaskType.UNKNOWN

        if not payload:
            issues.append("structured_output.empty")
            return issues

        schema_error = self._validate_schema(task_type, payload)
        if schema_error:
            issues.append(schema_error)

        if task_type == OrchestrationTaskType.REVIEW_PATIENT:
            metrics = payload.get("deviation_metrics") or {}
            gait_block = payload.get("gait_explanation") or {}
            driver_flags = metrics.get("driver_flags") or []
            reflection = payload.get("reflection") or {}
            if any("gait" in str(flag).lower() or "walk" in str(flag).lower() for flag in driver_flags):
                issues.append("guardrail.b_chain_affects_risk_score")
            if gait_block and gait_block.get("available") and not gait_block.get("note"):
                issues.append("guardrail.gait_scope_note_missing")
            if reflection.get("missing_fields"):
                issues.append("guardrail.missing_core_evidence")
            execution_summary = payload.get("execution_summary") or {}
            outcome_change = payload.get("outcome_change") or {}
            if execution_summary.get("log_count", 0) == 0 and outcome_change.get("report_count", 0) == 0:
                issues.append("guardrail.evidence_chain_incomplete")
        elif task_type in {OrchestrationTaskType.SCREEN_RISK, OrchestrationTaskType.WEEKLY_REPORT}:
            if "gait_explanation" in payload:
                issues.append("guardrail.b_chain_block_present_in_group_output")
            if (payload.get("patient_count") or 0) == 0:
                issues.append("guardrail.empty_patient_cohort")

        source_backend = payload.get("source_backend")
        if source_backend == "mock" and any(
            keyword in (state.final_text or "")
            for keyword in ("真实数据库", "真实 MySQL", "mysql confirmed")
        ):
            issues.append("guardrail.mock_backend_misreported_as_mysql")

        final_text = state.final_text or ""
        if any(phrase in final_text for phrase in UNRELIABLE_PHRASES):
            issues.append("guardrail.unreliable_phrase_in_final_text")

        if task_type == OrchestrationTaskType.REVIEW_PATIENT:
            risk_level = (payload.get("deviation_metrics") or {}).get("risk_level")
            conflicting_labels = {
                "low": ("高风险", "中风险", "high risk", "medium risk"),
                "medium": ("低风险", "高风险", "low risk", "high risk"),
                "high": ("低风险", "中风险", "low risk", "medium risk"),
            }
            for label in conflicting_labels.get(str(risk_level), ()):
                if label in final_text:
                    issues.append("guardrail.final_text_conflicts_with_structured_risk_level")
                    break

        for step_result in state.step_results:
            if not step_result.success and step_result.error:
                issues.append(f"execution.{step_result.step_id}.failed")
        return issues

    def render_final_text(self, state: OrchestrationState) -> str:
        payload = state.structured_output or {}
        task_type = state.intent.task_type if state.intent else OrchestrationTaskType.UNKNOWN

        if task_type == OrchestrationTaskType.REVIEW_PATIENT:
            reflection = payload.get("reflection") or {}
            gait = payload.get("gait_explanation") or {}
            metrics = payload.get("deviation_metrics") or {}
            outcome = payload.get("outcome_change") or {}
            focus = "\n".join(f"- {item}" for item in payload.get("review_focus", [])) or "- 无"
            interventions = "\n".join(f"- {item}" for item in payload.get("initial_interventions", [])) or "- 无"
            return (
                "单患者复核\n"
                f"患者: {payload.get('patient_id')}\n"
                f"计划: {payload.get('primary_plan_id')}\n"
                f"时间范围: {self._localize_text((payload.get('time_range') or {}).get('label'))}\n"
                f"风险等级: {self._localize_risk_level(metrics.get('risk_level'))} ({metrics.get('risk_score')})\n"
                f"偏离摘要: {self._localize_text(metrics.get('summary_text'))}\n"
                f"结果变化: {self._localize_text(outcome.get('summary_text'))}\n"
                f"步态证据: {self._localize_text(gait.get('note'))}\n"
                f"复核重点:\n{focus}\n"
                f"介入建议:\n{interventions}\n"
                f"人工确认: {self._localize_bool(reflection.get('recommend_manual_confirmation'))}\n"
                f"综述: {self._localize_text(payload.get('narrative_summary'))}"
            )

        if task_type == OrchestrationTaskType.SCREEN_RISK:
            patients = payload.get("patients") or []
            lines = [
                "多患者风险筛选",
                f"治疗师: {payload.get('therapist_id')}",
                f"时间范围: {self._localize_text((payload.get('time_range') or {}).get('label'))}",
                f"总患者数: {payload.get('patient_count')}",
                f"返回患者数: {payload.get('selected_count')}",
                f"摘要: {self._localize_text(payload.get('summary_text'))}",
            ]
            for index, item in enumerate(patients, start=1):
                lines.append(
                    f"{index}. 患者{item.get('patient_id')} | 风险 {self._localize_risk_level(item.get('risk_level'))} ({item.get('risk_score')}) | {self._localize_text(item.get('summary'))}"
                )
            for item in payload.get("review_card_summaries") or []:
                lines.append(
                    f"- 重点复核: 患者{item.get('patient_id')} | 计划{item.get('primary_plan_id')} | {self._localize_text(item.get('narrative_summary'))}"
                )
            return "\n".join(lines)

        if task_type == OrchestrationTaskType.WEEKLY_REPORT:
            lines = [
                "周报 / 风险摘要",
                f"治疗师: {payload.get('therapist_id')}",
                f"时间范围: {self._localize_text((payload.get('time_range') or {}).get('label'))}",
                f"患者数: {payload.get('patient_count')}",
                f"高风险: {payload.get('high_risk_count')}",
                f"中风险: {payload.get('medium_risk_count')}",
                f"低风险: {payload.get('low_risk_count')}",
                f"优先关注: {payload.get('priority_patient_ids')}",
                f"摘要: {self._localize_text(payload.get('summary_text'))}",
            ]
            for item in payload.get("patients") or []:
                lines.append(
                    f"- 患者{item.get('patient_id')} | 风险 {self._localize_risk_level(item.get('risk_level'))} ({item.get('risk_score')}) | {self._localize_text(item.get('summary'))}"
                )
            return "\n".join(lines)

        if task_type == OrchestrationTaskType.GAIT_REVIEW:
            lines = [
                "步态专项证据",
                f"患者: {payload.get('patient_id')}",
                f"时间范围: {self._localize_text((payload.get('time_range') or {}).get('label'))}",
                f"可用: {self._localize_bool(payload.get('available'))}",
                f"说明: {self._localize_text(payload.get('note'))}",
            ]
            for item in payload.get("sessions") or []:
                lines.append(
                    f"- 步道计划{item.get('walk_plan_id')} | 完成率 {item.get('completion_rate')} | 正确率 {item.get('correct_rate')} | {self._localize_text(item.get('explanation'))}"
                )
            return "\n".join(lines)

        return "当前仅支持单患者复核（review_patient）、风险筛选（screen_risk）、周报生成（weekly_report）和预留的步态复核（gait_review）。"

    def _call_tool(self, tool_name: str, args: dict[str, Any], mode: ExecutionMode) -> Any:
        tool = self._allowed_tools().get(tool_name)
        if tool is None:
            raise ValueError(f"tool_not_allowed:{tool_name}")
        return tool.invoke(mode=mode, args=args)

    def _allowed_tools(self) -> dict[str, ToolSpec]:
        return self.tool_registry

    def _build_default_constraints(self) -> list[str]:
        return [
            "风险评分只能使用 A 链指标",
            "B 链证据不能影响风险评分",
            "禁止原始 SQL",
            "禁止臆造 schema",
            "只能使用 service 层与 tools 白名单能力",
        ]

    def _resolve_mode(self, request: OrchestratorRequest, llm_config: ResolvedLLMConfig) -> tuple[ExecutionMode, str, list[str]]:
        requested_agent_sdk = bool(request.use_agent_sdk)
        if requested_agent_sdk and llm_config.can_use_agents_sdk:
            return "agents_sdk", "agents_sdk", []
        if requested_agent_sdk and not llm_config.can_use_agents_sdk:
            return "direct", "direct_fallback", ["execution_mode.fallback_to_direct"]
        return "direct", "direct", []

    def _extract_slots(self, text: str) -> dict[str, Any]:
        if not text:
            return {}
        lowered = text.lower()
        return {
            "patient_id": self._extract_identifier(text, ("患者", "病人", "patient")),
            "plan_id": self._extract_identifier(text, ("计划", "plan")),
            "therapist_id": self._extract_identifier(text, ("医生", "治疗师", "康复师", "doctor", "therapist")),
            "days": self._extract_days(text),
            "top_k": self._extract_top_k(text),
            "need_gait_evidence": any(keyword in text or keyword in lowered for keyword in GAIT_KEYWORDS),
            "response_style": self._extract_response_style(text),
        }

    def _infer_task_type(self, text: str, context: dict[str, Any]) -> OrchestrationTaskType:
        lowered = text.lower()
        if any(keyword in text or keyword in lowered for keyword in GAIT_KEYWORDS) and "周报" not in text and "risk" not in lowered:
            if self._extract_identifier(text, ("患者", "病人", "patient")) is not None:
                return OrchestrationTaskType.GAIT_REVIEW
        if any(keyword in text or keyword in lowered for keyword in WEEKLY_KEYWORDS):
            return OrchestrationTaskType.WEEKLY_REPORT
        if any(keyword in text or keyword in lowered for keyword in SCREEN_KEYWORDS):
            return OrchestrationTaskType.SCREEN_RISK
        if self._extract_identifier(text, ("计划", "plan")) is not None or self._extract_identifier(text, ("患者", "病人", "patient")) is not None:
            return OrchestrationTaskType.REVIEW_PATIENT
        if any(keyword in text or keyword in lowered for keyword in REVIEW_KEYWORDS):
            return OrchestrationTaskType.REVIEW_PATIENT
        if self._is_follow_up(text):
            return normalize_task_type(context.get("task_type"))
        return OrchestrationTaskType.UNKNOWN

    def _resolve_step_args(self, step: PlanStep, state: OrchestrationState) -> dict[str, Any]:
        args = dict(step.args)
        if step.tool_name == "generate_review_card" and "_patient_ids_from" in args:
            source_step_id = args.pop("_patient_ids_from")
            top_n = int(args.pop("_top_n", 3))
            source_result = self._find_step_result(state, source_step_id)
            patient_ids: list[int] = []
            if source_result and isinstance(source_result.raw_output, dict):
                patients = source_result.raw_output.get("patients") or []
                patient_ids = [
                    item.get("patient_id")
                    for item in patients[:top_n]
                    if isinstance(item, dict) and item.get("patient_id") is not None
                ]
            args["patient_ids"] = patient_ids
        if step.tool_name == "reflect_on_output":
            args.setdefault("task_type", state.intent.task_type.value if state.intent else "unknown")
            args.setdefault("current_output", state.structured_output)
        return args

    def _merge_step_output(self, state: OrchestrationState, step: PlanStep, raw_output: Any) -> None:
        if step.tool_name == "reflect_on_output":
            if isinstance(raw_output, dict):
                issues = raw_output.get("issues") or []
                for issue in issues:
                    state.validation_issues.append(f"reflection.{issue}")
            return

        if step.tool_name == "generate_review_card" and isinstance(raw_output, list):
            if state.structured_output is None:
                state.structured_output = {"review_card_summaries": raw_output}
                return
            state.structured_output["review_card_summaries"] = [
                {
                    "patient_id": item.get("patient_id"),
                    "primary_plan_id": item.get("primary_plan_id"),
                    "narrative_summary": item.get("narrative_summary"),
                    "review_focus": item.get("review_focus"),
                    "initial_interventions": item.get("initial_interventions"),
                }
                for item in raw_output
                if isinstance(item, dict)
            ]
            return

        if isinstance(raw_output, dict):
            state.structured_output = raw_output

    def _validate_schema(self, task_type: OrchestrationTaskType, payload: dict[str, Any]) -> str | None:
        try:
            if task_type == OrchestrationTaskType.REVIEW_PATIENT:
                ReviewCard.model_validate(payload)
            elif task_type == OrchestrationTaskType.SCREEN_RISK:
                RiskScreenOutput.model_validate(payload)
            elif task_type == OrchestrationTaskType.WEEKLY_REPORT:
                WeeklyRiskReport.model_validate(payload)
            elif task_type == OrchestrationTaskType.GAIT_REVIEW:
                GaitExplanationSummary.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            return f"schema.invalid:{exc}"
        return None

    def _summarize_tool_output(self, tool_name: str, raw_output: Any) -> str:
        if isinstance(raw_output, dict):
            if tool_name == "generate_review_card":
                metrics = raw_output.get("deviation_metrics") or {}
                return f"复核卡已生成，风险等级={self._localize_risk_level(metrics.get('risk_level'))}，风险分={metrics.get('risk_score')}"
            if tool_name == "screen_risk_patients":
                return f"已完成风险筛选，返回 {raw_output.get('selected_count')} 名患者"
            if tool_name == "generate_weekly_risk_report":
                return f"周报已生成，高风险患者数={raw_output.get('high_risk_count')}"
            if tool_name == "get_gait_explanation":
                return f"步态证据可用={self._localize_bool(raw_output.get('available'))}"
            if tool_name == "reflect_on_output":
                return str(raw_output.get("summary_text") or "护栏检查已完成")
            return f"返回字典字段={sorted(raw_output.keys())}"
        if isinstance(raw_output, list):
            return f"返回列表项数={len(raw_output)}"
        return type(raw_output).__name__

    def _find_step_result(self, state: OrchestrationState, step_id: str) -> StepExecutionResult | None:
        for item in state.step_results:
            if item.step_id == step_id:
                return item
        return None

    def _localize_text(self, value: Any) -> str:
        text = "" if value is None else str(value)
        replacements = [
            (r"\blow risk\b", "低风险"),
            (r"\bmedium risk\b", "中风险"),
            (r"\bhigh risk\b", "高风险"),
            (r"\blow\b", "低"),
            (r"\bmedium\b", "中"),
            (r"\bhigh\b", "高"),
            (r"\bstable\b", "稳定"),
            (r"\bimproving\b", "改善中"),
            (r"\bimproved\b", "已改善"),
            (r"\bworsening\b", "恶化中"),
            (r"\bdecline\b", "下降"),
            (r"\bdecreasing\b", "下降中"),
            (r"\bincrease\b", "上升"),
            (r"\bincreasing\b", "上升中"),
            (r"\bto\b", "至"),
            (r"\bTrue\b", "是"),
            (r"\bFalse\b", "否"),
        ]
        for pattern, target in replacements:
            text = re.sub(pattern, target, text, flags=re.IGNORECASE)
        return text

    def _localize_risk_level(self, value: Any) -> str:
        mapping = {
            "low": "低",
            "medium": "中",
            "high": "高",
            "low risk": "低风险",
            "medium risk": "中风险",
            "high risk": "高风险",
        }
        if value is None:
            return "未知"
        return mapping.get(str(value).strip().lower(), str(value))

    def _localize_bool(self, value: Any) -> str:
        if value is True:
            return "是"
        if value is False:
            return "否"
        return str(value)

    def _render_unsupported(self, intent: OrchestrationIntent) -> str:
        if intent.missing_slots:
            return (
                f"无法完成请求，缺少必要字段: {', '.join(intent.missing_slots)}。"
                "当前支持单患者复核（review_patient）、风险筛选（screen_risk）、周报生成（weekly_report）和预留的步态复核（gait_review）。"
            )
        return "当前仅支持单患者复核（review_patient）、风险筛选（screen_risk）、周报生成（weekly_report）和预留的步态复核（gait_review）。"

    def _build_response(
        self,
        state: OrchestrationState,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        *,
        success: bool,
    ) -> OrchestratorResponse:
        return OrchestratorResponse(
            success=success,
            task_type=state.intent.task_type.value if state.intent else OrchestrationTaskType.UNKNOWN.value,
            execution_mode=execution_mode,
            llm_provider=llm_config.provider,
            llm_model=llm_config.model,
            structured_output=state.structured_output or {},
            final_text=state.final_text or "",
            validation_issues=state.validation_issues,
            execution_trace=state.step_results,
        )

    def _extract_identifier(self, text: str, labels: tuple[str, ...]) -> int | None:
        for label in labels:
            pattern = rf"{re.escape(label)}\s*(?:id)?\s*[:：]?\s*(\d+)"
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    def _extract_days(self, text: str) -> int | None:
        lowered = text.lower()
        if "本周" in text or "最近一周" in text or "近一周" in text or "last week" in lowered:
            return 7
        if "本月" in text or "最近一个月" in text or "近一个月" in text or "last month" in lowered:
            return 30
        for pattern in (
            r"(?:最近|过去|近)\s*(\d+)\s*天",
            r"last\s*(\d+)\s*days?",
            r"(\d+)\s*天",
        ):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    def _extract_top_k(self, text: str) -> int | None:
        for pattern in (r"top\s*(\d+)", r"前\s*(\d+)", r"(\d+)\s*个"):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    def _extract_response_style(self, text: str) -> str | None:
        lowered = text.lower()
        if any(keyword in text or keyword in lowered for keyword in DETAIL_KEYWORDS):
            return "detailed"
        if any(keyword in text or keyword in lowered for keyword in BRIEF_KEYWORDS):
            return "brief"
        return None

    def _is_follow_up(self, text: str) -> bool:
        lowered = text.lower()
        return any(keyword in text or keyword in lowered for keyword in FOLLOW_UP_KEYWORDS)

    def _coalesce(self, *values: Any) -> Any:
        for value in values:
            if value is not None:
                return value
        return None
