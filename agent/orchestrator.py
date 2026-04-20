from __future__ import annotations

import re
from typing import Any

from config import ResolvedLLMConfig, Settings, get_settings
from models import GaitExplanationSummary, ReviewCard, SessionIdentityContext, WeeklyRiskReport
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
    ResultSetService,
    UserLookupService,
)
from server.result_set_store import ResultSetStore, get_result_set_store
from tools import (
    ToolSpec,
    build_analytics_tools,
    build_execution_tools,
    build_gait_tools,
    build_outcome_tools,
    build_plan_tools,
    build_reflection_tools,
    build_report_tools,
    build_result_set_tools,
    build_user_lookup_tools,
    build_tool_registry,
)
from .analytics_manager import AnalyticsManager
from .intent_router import IntentRouter
from .llm_planner import LLMPlanner
from .llm_router import LLMRouter, merge_rule_and_llm
from .plan_validator import PlanValidator
from .roster_query import extract_limit, has_patient_roster_subject

from .schemas import (
    ExecutionStrategy,
    ExecutionMode,
    OrchestrationIntent,
    OrchestrationPlan,
    OrchestrationState,
    OrchestrationTaskType,
    OrchestratorRequest,
    OrchestratorResponse,
    PlanStep,
    RiskScreenOutput,
    RoutedDecision,
    StepExecutionResult,
    normalize_task_type,
)


class FixedWorkflowEntryError(RuntimeError):
    """Raised when fixed_workflow is reached without explicit eligibility."""


WEEKLY_KEYWORDS = ("周报", "weekly", "summary", "摘要")
SCREEN_KEYWORDS = ("高风险", "风险筛选", "优先复核", "risk", "screen")
REVIEW_KEYWORDS = ("复核", "计划", "患者", "病人", "review", "plan", "patient")
GAIT_KEYWORDS = ("步态", "步道", "gait", "walkway", "walk")
DETAIL_KEYWORDS = ("详细", "原因", "detail", "detailed", "reason", "why")
BRIEF_KEYWORDS = ("简短", "简洁", "brief", "short")
FOLLOW_UP_KEYWORDS = ("换成", "改成", "调整", "继续", "this", "same", "switch", "change")
UNRELIABLE_PHRASES = ("根据数据库推测", "大概", "猜测", "可能是数据库显示")
FIXED_WORKFLOW_INTENTS = {"single_patient_review", "risk_screening", "weekly_report", "gait_review"}
FIXED_WORKFLOW_TASKS = {
    OrchestrationTaskType.REVIEW_PATIENT,
    OrchestrationTaskType.SCREEN_RISK,
    OrchestrationTaskType.WEEKLY_REPORT,
    OrchestrationTaskType.GAIT_REVIEW,
}


class RehabAgentOrchestrator:
    def __init__(self, settings: Settings | None = None, *, result_set_store: ResultSetStore | None = None):
        self.settings = settings or get_settings()
        self.intent_router = IntentRouter()
        self.llm_router = LLMRouter(settings=self.settings)
        self.result_set_store = result_set_store or get_result_set_store()

        self.repository = RehabRepository(self.settings)
        self.analytics_service = AnalyticsService(self.repository, self.settings)
        self.plan_service = PlanService(self.repository, self.settings)
        self.execution_service = ExecutionService(self.repository, self.settings)
        self.outcome_service = OutcomeService(self.repository, self.settings)
        self.gait_service = GaitService(self.repository, self.settings)
        self.deviation_service = DeviationService(self.settings)
        self.reflection_service = ReflectionService()
        self.user_lookup_service = UserLookupService(self.repository, self.settings, self.result_set_store)
        self.result_set_service = ResultSetService(self.repository, self.result_set_store)
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
        self.user_lookup_tools = build_user_lookup_tools(self.user_lookup_service)
        self.result_set_tools = build_result_set_tools(self.result_set_service)
        self.analytics_tools = build_analytics_tools(self.analytics_service) + self.user_lookup_tools + self.result_set_tools
        self.analytics_tool_registry = build_tool_registry(self.analytics_tools)
        self.llm_planner = LLMPlanner(settings=self.settings)
        self.plan_validator = PlanValidator(tool_registry=self.analytics_tool_registry)
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
            analytics_tool_registry=self.analytics_tool_registry,
            settings=self.settings,
            llm_planner=self.llm_planner,
            plan_validator=self.plan_validator,
        )

    def run(self, request: OrchestratorRequest) -> OrchestratorResponse:
        request, identity_error = self._normalize_identity_context(request)
        llm_config = self.settings.resolve_llm_config(
            provider=request.llm_provider,
            model=request.llm_model,
            base_url=request.llm_base_url,
        )
        if identity_error is not None:
            return self._build_identity_error_response(request, llm_config, identity_error)
        request = self._attach_working_context(request)
        self.repository.set_identity_context(request.identity_context)
        identity_trace = self._build_identity_trace(request.identity_context)
        mode, execution_mode, mode_issues = self._resolve_mode(request, llm_config)
        rule_decision = self.intent_router.route(request)
        routed = self._refine_intent_with_llm_if_needed(
            request=request,
            rule_decision=rule_decision,
            llm_config=llm_config,
            mode=mode,
        )
        route_trace = self._build_route_trace(routed)
        authorization_issue = self._authorize_request(request, routed)
        if authorization_issue is not None:
            return self._build_authorization_error_response(
                request=request,
                llm_config=llm_config,
                execution_mode=execution_mode,
                issue=authorization_issue,
                traces=[identity_trace, route_trace],
                mode_issues=mode_issues,
            )
        if routed.final_intent == "result_set_query":
            return self._run_result_set_query(
                request=request,
                routed=routed,
                identity_trace=identity_trace,
                route_trace=route_trace,
                llm_config=llm_config,
                execution_mode=execution_mode,
                mode_issues=mode_issues,
            )
        if routed.final_intent == "lookup_query":
            return self._run_lookup_query(
                request=request,
                routed=routed,
                identity_trace=identity_trace,
                route_trace=route_trace,
                llm_config=llm_config,
                execution_mode=execution_mode,
                mode_issues=mode_issues,
            )
        strategy = self.choose_execution_strategy(request, routed, mode=mode, llm_config=llm_config)
        strategy_trace = self._build_strategy_trace(strategy)
        if strategy.kind == "fixed_workflow":
            return self._run_fixed_workflow(
                request=request,
                routed=routed,
                route_trace=route_trace,
                strategy_trace=strategy_trace,
                identity_trace=identity_trace,
                mode=mode,
                llm_config=llm_config,
                execution_mode=execution_mode,
                mode_issues=mode_issues,
            )

        response = self.analytics_manager.run(
            request=request,
            routed_decision=routed,
            strategy=strategy,
            mode=mode,
            llm_config=llm_config,
            execution_mode=execution_mode,
        )
        response.validation_issues = list(mode_issues) + list(response.validation_issues)
        response.execution_trace = [identity_trace, route_trace, strategy_trace] + list(response.execution_trace)
        return response

    def _normalize_identity_context(self, request: OrchestratorRequest) -> tuple[OrchestratorRequest, str | None]:
        identity = request.identity_context or self._identity_from_request_fields(request)
        if identity is None:
            return request, "missing_identity_context"
        update: dict[str, Any] = {"identity_context": identity}
        if identity.actor_role == "doctor":
            doctor_id = identity.actor_doctor_id
            if doctor_id is None:
                return request, "identity_context.missing_actor_doctor_id"
            requested_doctor_id = request.doctor_id or request.therapist_id
            if requested_doctor_id is not None and int(requested_doctor_id) != int(doctor_id):
                return request, "authorization.doctor_scope_violation"
            update["doctor_id"] = doctor_id
            update["therapist_id"] = doctor_id
            if request.patient_id is not None and identity.target_patient_id is None:
                identity = identity.model_copy(update={"target_patient_id": request.patient_id})
                update["identity_context"] = identity
            elif request.patient_id is None and identity.target_patient_id is not None:
                update["patient_id"] = identity.target_patient_id
        else:
            patient_id = identity.actor_patient_id
            if patient_id is None:
                return request, "identity_context.missing_actor_patient_id"
            if request.patient_id is not None and int(request.patient_id) != int(patient_id):
                return request, "authorization.patient_scope_violation"
            if request.patient_id is None:
                update["patient_id"] = patient_id
            update["doctor_id"] = None
            update["therapist_id"] = None
        return request.model_copy(update=update), None

    def _attach_working_context(self, request: OrchestratorRequest) -> OrchestratorRequest:
        context = self.result_set_store.apply_to_context(
            request.identity_context,
            request.context or {},
        )
        return request.model_copy(update={"context": context})

    def _identity_from_request_fields(self, request: OrchestratorRequest) -> SessionIdentityContext | None:
        doctor_id = request.doctor_id or request.therapist_id
        patient_id = request.patient_id
        if doctor_id is None and patient_id is None:
            return None
        if doctor_id is not None:
            return SessionIdentityContext(
                actor_role="doctor",
                actor_doctor_id=int(doctor_id),
                target_doctor_id=int(doctor_id),
                target_patient_id=int(patient_id) if patient_id is not None else None,
            )
        return SessionIdentityContext(
            actor_role="patient",
            actor_patient_id=int(patient_id),  # type: ignore[arg-type]
            target_patient_id=int(patient_id),  # type: ignore[arg-type]
        )

    def _build_identity_trace(self, identity: SessionIdentityContext | None) -> StepExecutionResult:
        return StepExecutionResult(
            step_id="session_identity",
            tool_name="identity_context",
            success=identity is not None,
            args={},
            output_summary=f"actor_role={identity.actor_role}" if identity else "missing identity context",
            raw_output=identity.model_dump(mode="json") if identity else None,
            error=None if identity else "missing_identity_context",
        )

    def _build_identity_error_response(
        self,
        request: OrchestratorRequest,
        llm_config: ResolvedLLMConfig,
        issue: str,
    ) -> OrchestratorResponse:
        trace = StepExecutionResult(
            step_id="session_identity",
            tool_name="identity_context",
            success=False,
            args={},
            output_summary="request rejected before routing",
            raw_output=None,
            error=issue,
        )
        return OrchestratorResponse(
            success=False,
            task_type=normalize_task_type(request.task_type).value,
            execution_mode="not_started",
            llm_provider=llm_config.provider,
            llm_model=llm_config.model,
            structured_output={"error": issue},
            final_text=(
                "缺少身份上下文：请求必须包含 doctor_id 或 patient_id。"
                if issue == "missing_identity_context"
                else "当前请求的身份字段与会话身份上下文不一致。"
            ),
            validation_issues=[issue],
            execution_trace=[trace],
        )

    def _authorize_request(self, request: OrchestratorRequest, routed: RoutedDecision) -> str | None:
        identity = request.identity_context
        if identity is None:
            return "missing_identity_context"
        normalized_task = normalize_task_type(request.task_type)
        extracted = self._extract_slots(request.raw_text or "")
        target_patient_id = request.patient_id or extracted.get("patient_id")
        target_plan_id = request.plan_id or extracted.get("plan_id")
        if routed.final_intent in {"lookup_query", "result_set_query"}:
            return None
        if identity.actor_role == "patient":
            actor_patient_id = identity.actor_patient_id
            if actor_patient_id is None:
                return "authorization.missing_actor_patient_id"
            if target_patient_id is not None and int(target_patient_id) != int(actor_patient_id):
                return "authorization.patient_scope_violation"
            if normalized_task in {OrchestrationTaskType.SCREEN_RISK, OrchestrationTaskType.WEEKLY_REPORT}:
                return "authorization.patient_cannot_run_group_workflow"
            if routed.final_intent in {"risk_screening", "weekly_report"}:
                return "authorization.patient_cannot_run_group_workflow"
            if routed.final_scope == "doctor_aggregate":
                return "authorization.patient_cannot_run_doctor_aggregate"
            if target_plan_id is not None and not self.repository.get_plan_records(plan_id=target_plan_id, patient_id=actor_patient_id, limit=1):
                return "authorization.patient_cannot_access_plan"
            return None

        actor_doctor_id = identity.actor_doctor_id
        if actor_doctor_id is None:
            return "authorization.missing_actor_doctor_id"
        if request.doctor_id is not None and int(request.doctor_id) != int(actor_doctor_id):
            return "authorization.doctor_scope_violation"
        if request.therapist_id is not None and int(request.therapist_id) != int(actor_doctor_id):
            return "authorization.doctor_scope_violation"
        if target_plan_id is not None and not self.repository.get_plan_records(plan_id=target_plan_id, therapist_id=actor_doctor_id, limit=1):
            return "authorization.doctor_cannot_access_plan"
        if target_patient_id is not None and not self._doctor_can_access_patient(actor_doctor_id, target_patient_id):
            return "authorization.doctor_cannot_access_patient"
        return None

    def _authorize_lookup_request(self, identity: SessionIdentityContext, routed: RoutedDecision) -> str | None:
        user_id = routed.lookup_user_id
        entity_type = routed.lookup_entity_type or "unknown"
        if user_id is None:
            return None

        if identity.actor_role == "patient":
            actor_patient_id = identity.actor_patient_id
            if actor_patient_id is None:
                return "authorization.missing_actor_patient_id"
            if entity_type in {"patient", "unknown"} and int(user_id) == int(actor_patient_id):
                return None
            return "authorization.patient_cannot_access_lookup_target"

        actor_doctor_id = identity.actor_doctor_id
        if actor_doctor_id is None:
            return "authorization.missing_actor_doctor_id"
        if entity_type == "doctor":
            if int(user_id) == int(actor_doctor_id):
                return None
            if (identity.authorized_scope or {}).get("allow_doctor_lookup"):
                return None
            return "authorization.doctor_cannot_lookup_other_doctor"
        if entity_type == "patient":
            if self._doctor_can_access_patient(actor_doctor_id, user_id):
                return None
            return "authorization.doctor_cannot_access_patient"
        if int(user_id) == int(actor_doctor_id) or self._doctor_can_access_patient(actor_doctor_id, user_id):
            return None
        return "authorization.lookup_entity_ambiguous"

    def _doctor_can_access_patient(self, doctor_id: int, patient_id: int) -> bool:
        plan_rows = self.repository.get_plan_records(patient_id=patient_id, therapist_id=doctor_id, limit=1)
        if plan_rows:
            return True
        execution_rows = self.repository.get_execution_logs(patient_id=patient_id, therapist_id=doctor_id, limit=1)
        return bool(execution_rows)

    def _run_lookup_query(
        self,
        *,
        request: OrchestratorRequest,
        routed: RoutedDecision,
        identity_trace: StepExecutionResult,
        route_trace: StepExecutionResult,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        mode_issues: list[str],
    ) -> OrchestratorResponse:
        subtype = routed.lookup_subtype or "lookup_user_name"
        if subtype == "list_my_patients":
            return self._run_roster_lookup_query(
                request=request,
                subtype=subtype,
                tool_name="list_my_patients",
                identity_trace=identity_trace,
                route_trace=route_trace,
                llm_config=llm_config,
                execution_mode=execution_mode,
                mode_issues=mode_issues,
            )
        if subtype == "list_my_doctors":
            return self._run_roster_lookup_query(
                request=request,
                subtype=subtype,
                tool_name="list_my_doctors",
                identity_trace=identity_trace,
                route_trace=route_trace,
                llm_config=llm_config,
                execution_mode=execution_mode,
                mode_issues=mode_issues,
            )

        user_id = routed.lookup_user_id or self._default_lookup_user_id(request)
        entity_type = self._default_lookup_entity_type(request, routed.lookup_entity_type)
        if user_id is None:
            lookup_trace = StepExecutionResult(
                step_id="lookup_user_name",
                tool_name="lookup_accessible_user_name",
                success=False,
                args={"entity_type": entity_type},
                output_summary="missing lookup user_id",
                raw_output=None,
                error="lookup.missing_user_id",
            )
            return OrchestratorResponse(
                success=False,
                task_type=OrchestrationTaskType.LOOKUP_QUERY.value,
                execution_mode=execution_mode,
                llm_provider=llm_config.provider,
                llm_model=llm_config.model,
                structured_output={"error": "lookup.missing_user_id", "lookup_subtype": subtype},
                final_text="缺少要查询的用户 ID，无法完成姓名查询。",
                validation_issues=list(mode_issues) + ["lookup.missing_user_id"],
                execution_trace=[identity_trace, route_trace, lookup_trace],
            )

        tool = self.analytics_tool_registry["lookup_accessible_user_name"]
        output = tool.invoke(mode="direct", args={"user_id": int(user_id)})
        label = self._lookup_entity_label(output.get("user_role") or entity_type, int(user_id))
        success = bool(output.get("is_accessible") and output.get("user_name"))
        lookup_trace = StepExecutionResult(
            step_id="lookup_user_name",
            tool_name="lookup_accessible_user_name",
            success=success,
            args={"entity_type": entity_type, "user_id": user_id},
            output_summary=f"{label} -> {output.get('user_name') or 'not_accessible_or_name_missing'}",
            raw_output=output,
            error=None if success else output.get("reason") or "lookup.not_accessible_or_not_found",
        )
        final_text = f"{label} 的姓名是 {output.get('user_name')}。" if success else "未找到可访问的用户信息。"
        structured_output = {
            "lookup_subtype": subtype,
            "entity_type": output.get("user_role") or entity_type,
            "display_label": label,
            "source_tool": "lookup_accessible_user_name",
            **output,
        }
        return OrchestratorResponse(
            success=success,
            task_type=OrchestrationTaskType.LOOKUP_QUERY.value,
            execution_mode=execution_mode,
            llm_provider=llm_config.provider,
            llm_model=llm_config.model,
            structured_output=structured_output,
            final_text=final_text,
            validation_issues=list(mode_issues) if success else list(mode_issues) + [lookup_trace.error or "lookup.not_accessible_or_not_found"],
            execution_trace=[identity_trace, route_trace, lookup_trace],
        )

    def _run_roster_lookup_query(
        self,
        *,
        request: OrchestratorRequest,
        subtype: str,
        tool_name: str,
        identity_trace: StepExecutionResult,
        route_trace: StepExecutionResult,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        mode_issues: list[str],
    ) -> OrchestratorResponse:
        days = self._lookup_days(request)
        args = {"days": days} if days is not None else {}
        tool = self.analytics_tool_registry[tool_name]
        output = tool.invoke(mode="direct", args=args)
        success = bool(output.get("is_accessible"))
        lookup_trace = StepExecutionResult(
            step_id=subtype,
            tool_name=tool_name,
            success=success,
            args=args,
            output_summary=f"{tool_name} count={output.get('count', 0)}",
            raw_output=output,
            error=None if success else output.get("reason") or "lookup.identity_scope_denied",
        )
        structured_output = {
            "lookup_subtype": subtype,
            "source_tool": tool_name,
            **output,
        }
        display_limit = self._roster_display_limit(request)
        rows = output.get("rows") or []
        structured_output["display_limit"] = display_limit
        structured_output["displayed_count"] = min(len(rows), display_limit)
        traces = [identity_trace, route_trace, lookup_trace]
        if success and output.get("active_result_set"):
            traces.append(self._result_set_registration_trace(output, source_tool=tool_name))
        return OrchestratorResponse(
            success=success,
            task_type=OrchestrationTaskType.LOOKUP_QUERY.value,
            execution_mode=execution_mode,
            llm_provider=llm_config.provider,
            llm_model=llm_config.model,
            structured_output=structured_output,
            final_text=self._render_roster_lookup(tool_name, output, limit=display_limit),
            validation_issues=list(mode_issues) if success else list(mode_issues) + [lookup_trace.error or "lookup.identity_scope_denied"],
            execution_trace=traces,
        )

    def _default_lookup_user_id(self, request: OrchestratorRequest) -> int | None:
        identity = request.identity_context
        if identity is None:
            return None
        if identity.actor_role == "doctor":
            return identity.actor_doctor_id
        return identity.actor_patient_id

    def _default_lookup_entity_type(self, request: OrchestratorRequest, entity_type: str | None) -> str:
        if entity_type and entity_type != "unknown":
            return entity_type
        identity = request.identity_context
        if identity is None:
            return "unknown"
        return identity.actor_role

    def _lookup_days(self, request: OrchestratorRequest) -> int | None:
        if request.days is not None:
            return int(request.days)
        extracted = self._extract_slots(request.raw_text or "")
        days = extracted.get("days")
        return int(days) if days is not None else None

    def _roster_display_limit(self, request: OrchestratorRequest) -> int:
        explicit_limit = extract_limit(request.raw_text or "")
        if explicit_limit is not None:
            return max(1, min(int(explicit_limit), 100))
        return max(1, min(int(request.top_k or 10), 100))

    def _render_roster_lookup(self, tool_name: str, output: dict[str, Any], *, limit: int) -> str:
        if not output.get("is_accessible"):
            return "当前身份无权执行该名单查询。"
        rows = output.get("rows") or []
        visible_rows = rows[:limit]
        if tool_name == "list_my_patients":
            lines = [f"共找到 {len(rows)} 名相关患者。"]
            lines.extend(f"- {self._patient_display(row)}" for row in visible_rows)
            return "\n".join(lines)
        lines = [f"共找到 {len(rows)} 名相关医生。"]
        lines.extend(f"- {self._doctor_display(row)}" for row in visible_rows)
        return "\n".join(lines)

    def _patient_display(self, row: dict[str, Any]) -> str:
        patient_id = row.get("patient_id")
        patient_name = row.get("patient_name")
        return str(patient_name) if patient_name else f"患者{patient_id}"

    def _doctor_display(self, row: dict[str, Any]) -> str:
        doctor_id = row.get("doctor_id")
        doctor_name = row.get("doctor_name")
        return str(doctor_name) if doctor_name else f"医生{doctor_id}"

    def _result_set_registration_trace(self, output: dict[str, Any], *, source_tool: str) -> StepExecutionResult:
        active = output.get("active_result_set") if isinstance(output, dict) else None
        active = active if isinstance(active, dict) else {}
        return StepExecutionResult(
            step_id="result_set_register",
            tool_name="result_set_store",
            success=True,
            args={"source_tool": source_tool, "result_set_type": active.get("result_set_type")},
            output_summary=f"active_result_set={active.get('result_set_id')}; count={active.get('count')}",
            raw_output=active,
        )

    def _lookup_entity_label(self, entity_type: str, user_id: int) -> str:
        if entity_type == "doctor":
            return f"医生{user_id}"
        if entity_type == "patient":
            return f"患者{user_id}"
        return f"用户{user_id}"

    def _build_authorization_error_response(
        self,
        *,
        request: OrchestratorRequest,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        issue: str,
        traces: list[StepExecutionResult],
        mode_issues: list[str],
    ) -> OrchestratorResponse:
        auth_trace = StepExecutionResult(
            step_id="authorization_scope",
            tool_name="authorization_guard",
            success=False,
            args={},
            output_summary="request rejected by session identity scope",
            raw_output=request.identity_context.model_dump(mode="json") if request.identity_context else None,
            error=issue,
        )
        return OrchestratorResponse(
            success=False,
            task_type=normalize_task_type(request.task_type).value,
            execution_mode=execution_mode,
            llm_provider=llm_config.provider,
            llm_model=llm_config.model,
            structured_output={"error": issue},
            final_text="当前身份上下文无权执行该请求或访问目标对象。",
            validation_issues=list(mode_issues) + [issue],
            execution_trace=traces + [auth_trace],
        )

    def _run_result_set_query(
        self,
        *,
        request: OrchestratorRequest,
        routed: RoutedDecision,
        identity_trace: StepExecutionResult,
        route_trace: StepExecutionResult,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        mode_issues: list[str],
    ) -> OrchestratorResponse:
        traces = [identity_trace, route_trace]
        active = self.result_set_store.get_active_ref(request.identity_context)
        if active is None and self._should_seed_patient_result_set(request):
            seed_artifact, seed_traces = self._seed_patient_result_set(request)
            active = self.result_set_store.get_active_ref(request.identity_context)
            traces.extend(seed_traces)

        if active is None:
            missing_trace = StepExecutionResult(
                step_id="active_result_set",
                tool_name="result_set_store",
                success=False,
                args={},
                output_summary="missing active result set for follow-up",
                raw_output=request.context,
                error="followup.missing_active_result_set",
            )
            return OrchestratorResponse(
                success=False,
                task_type=OrchestrationTaskType.RESULT_SET_QUERY.value,
                execution_mode=execution_mode,
                llm_provider=llm_config.provider,
                llm_model=llm_config.model,
                structured_output={"error": "followup.missing_active_result_set"},
                final_text="没有可继续操作的上一轮结果集，请先生成一个患者名单或结果列表。",
                validation_issues=list(mode_issues) + ["followup.missing_active_result_set"],
                execution_trace=traces + [missing_trace],
            )

        tool_name = self._result_set_tool_name(routed)
        if tool_name is None:
            unsupported_trace = StepExecutionResult(
                step_id="result_set_operation",
                tool_name="result_set_router",
                success=False,
                args=routed.model_dump(mode="json"),
                output_summary="unsupported result-set operation",
                raw_output=routed.model_dump(mode="json"),
                error="result_set.unsupported_operation",
            )
            return OrchestratorResponse(
                success=False,
                task_type=OrchestrationTaskType.RESULT_SET_QUERY.value,
                execution_mode=execution_mode,
                llm_provider=llm_config.provider,
                llm_model=llm_config.model,
                structured_output={"error": "result_set.unsupported_operation"},
                final_text="当前结果集操作还不支持。",
                validation_issues=list(mode_issues) + ["result_set.unsupported_operation"],
                execution_trace=traces + [unsupported_trace],
            )

        days = self._result_set_days(request, routed)
        args: dict[str, Any] = {"result_set_id": active.result_set_id}
        if tool_name != "enrich_result_set_with_completion_time" and days is not None:
            args["days"] = days
        tool = self.analytics_tool_registry[tool_name]
        try:
            output = tool.invoke(mode="direct", args=args)
        except Exception as exc:  # noqa: BLE001
            failure_trace = StepExecutionResult(
                step_id=tool_name,
                tool_name=tool_name,
                success=False,
                args=args,
                output_summary="result-set tool failed; active result set unchanged",
                raw_output=None,
                error=str(exc),
            )
            return OrchestratorResponse(
                success=False,
                task_type=OrchestrationTaskType.RESULT_SET_QUERY.value,
                execution_mode=execution_mode,
                llm_provider=llm_config.provider,
                llm_model=llm_config.model,
                structured_output={"error": "result_set.operation_failed", "reason": str(exc)},
                final_text="Result-set operation failed; active result set is unchanged.",
                validation_issues=list(mode_issues) + ["result_set.operation_failed"],
                execution_trace=traces + [failure_trace],
            )
        success = bool(output.get("is_accessible"))
        result_trace = StepExecutionResult(
            step_id=tool_name,
            tool_name=tool_name,
            success=success,
            args=args,
            output_summary=f"{tool_name} count={output.get('count', 0)}",
            raw_output=output,
            error=None if success else output.get("reason") or "result_set.operation_failed",
        )
        result_traces = traces + [result_trace]
        if success and output.get("active_result_set"):
            result_traces.append(self._result_set_registration_trace(output, source_tool=tool_name))
        return OrchestratorResponse(
            success=success,
            task_type=OrchestrationTaskType.RESULT_SET_QUERY.value,
            execution_mode=execution_mode,
            llm_provider=llm_config.provider,
            llm_model=llm_config.model,
            structured_output=output,
            final_text=self._render_result_set_output(output),
            validation_issues=list(mode_issues) if success else list(mode_issues) + [result_trace.error or "result_set.operation_failed"],
            execution_trace=result_traces,
        )

    def _should_seed_patient_result_set(self, request: OrchestratorRequest) -> bool:
        identity = request.identity_context
        if identity is None or identity.actor_role != "doctor":
            return False
        return has_patient_roster_subject(request.raw_text or "")

    def _seed_patient_result_set(self, request: OrchestratorRequest):
        days = self._lookup_days(request)
        args: dict[str, Any] = {"days": days} if days is not None else {}
        tool = self.analytics_tool_registry["list_my_patients"]
        output = tool.invoke(mode="direct", args=args)
        lookup_trace = StepExecutionResult(
            step_id="seed_active_result_set",
            tool_name="list_my_patients",
            success=bool(output.get("is_accessible")),
            args=args,
            output_summary=f"seed patient result set count={output.get('count', 0)}",
            raw_output=output,
            error=None if output.get("is_accessible") else output.get("reason") or "lookup.identity_scope_denied",
        )
        traces = [lookup_trace]
        if output.get("is_accessible") and output.get("active_result_set"):
            traces.append(self._result_set_registration_trace(output, source_tool="list_my_patients"))
        return output.get("active_result_set"), traces

    def _result_set_tool_name(self, routed: RoutedDecision) -> str | None:
        if routed.result_set_operation == "enrich" and routed.result_set_target_field == "completion_time":
            return "enrich_result_set_with_completion_time"
        if routed.result_set_operation == "filter":
            if routed.result_set_filter_kind == "training":
                return "filter_result_set_by_training"
            if routed.result_set_filter_kind == "absence":
                return "filter_result_set_by_absence"
            if routed.result_set_filter_kind == "plan_completion":
                return "filter_result_set_by_plan_completion"
        return None

    def _result_set_days(self, request: OrchestratorRequest, routed: RoutedDecision) -> int | None:
        if routed.days is not None:
            return int(routed.days)
        if request.days is not None:
            return int(request.days)
        extracted = self._extract_slots(request.raw_text or "")
        days = extracted.get("days")
        if days is not None:
            return int(days)
        return self.result_set_store.get_default_time_window_days(request.identity_context)

    def _render_result_set_output(self, output: dict[str, Any]) -> str:
        if not output.get("is_accessible"):
            return "当前身份上下文无权访问该结果集。"
        rows = output.get("rows") or []
        lines = [str(output.get("summary") or f"结果集包含 {len(rows)} 条记录。")]
        for index, row in enumerate(rows[:20], start=1):
            label = self._patient_display(row) if row.get("patient_id") is not None else self._doctor_display(row)
            details: list[str] = []
            if row.get("completion_time"):
                details.append(f"completion_time {row.get('completion_time')}")
            if row.get("last_training_time"):
                details.append(f"last_training_time {row.get('last_training_time')}")
            if row.get("training_count_in_window") is not None:
                details.append(f"training_count {row.get('training_count_in_window')}")
            if row.get("completed_plan_count_in_window") is not None:
                details.append(f"completed_plan_count {row.get('completed_plan_count_in_window')}")
            suffix = " | " + " | ".join(details) if details else ""
            lines.append(f"{index}. {label}{suffix}")
        return "\n".join(lines)

    def _run_fixed_workflow(
        self,
        *,
        request: OrchestratorRequest,
        routed: RoutedDecision,
        route_trace: StepExecutionResult,
        strategy_trace: StepExecutionResult,
        identity_trace: StepExecutionResult,
        mode: ExecutionMode,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        mode_issues: list[str],
    ) -> OrchestratorResponse:
        direct_request = request.model_copy(deep=True)
        if routed.final_intent == "single_patient_review":
            direct_request.task_type = OrchestrationTaskType.REVIEW_PATIENT.value
        elif routed.final_intent == "risk_screening":
            direct_request.task_type = OrchestrationTaskType.SCREEN_RISK.value
        elif routed.final_intent == "weekly_report":
            direct_request.task_type = OrchestrationTaskType.WEEKLY_REPORT.value
        elif routed.final_intent == "gait_review":
            direct_request.task_type = OrchestrationTaskType.GAIT_REVIEW.value
        state = OrchestrationState(
            user_query=direct_request.raw_text or "",
            mode=mode,
            validation_issues=list(mode_issues),
        )
        state.step_results.append(identity_trace)
        state.step_results.append(route_trace)
        state.step_results.append(strategy_trace)

        intent = self.route_intent(direct_request)
        state.intent = intent
        self._validate_fixed_workflow_entry(intent=intent, routed=routed)

        state.plan = self.build_plan(intent, mode)
        if not state.plan.steps:
            raise FixedWorkflowEntryError("fixed_workflow.empty_plan")
        self.execute_plan(state)
        state.final_text = self.render_final_text(state)
        state.validation_issues.extend(self.validate_output(state))
        success = bool(state.structured_output) and not any(
            issue.startswith("structured_output.") or issue.startswith("schema.")
            for issue in state.validation_issues
        )
        return self._build_response(state, llm_config, execution_mode, success=success)

    def _validate_fixed_workflow_entry(self, *, intent: OrchestrationIntent, routed: RoutedDecision) -> None:
        if routed.final_intent not in FIXED_WORKFLOW_INTENTS:
            raise FixedWorkflowEntryError("fixed_workflow.unsupported_entry")
        if intent.task_type not in FIXED_WORKFLOW_TASKS:
            raise FixedWorkflowEntryError("fixed_workflow.unsupported_entry")
        if not intent.missing_slots:
            return
        if intent.task_type == OrchestrationTaskType.REVIEW_PATIENT:
            raise FixedWorkflowEntryError("fixed_workflow.review_patient_missing_slots")
        if intent.task_type == OrchestrationTaskType.SCREEN_RISK:
            raise FixedWorkflowEntryError("fixed_workflow.screen_risk_missing_slots")
        if intent.task_type == OrchestrationTaskType.WEEKLY_REPORT:
            raise FixedWorkflowEntryError("fixed_workflow.weekly_report_missing_slots")
        if intent.task_type == OrchestrationTaskType.GAIT_REVIEW:
            raise FixedWorkflowEntryError("fixed_workflow.gait_review_missing_slots")
        raise FixedWorkflowEntryError("fixed_workflow.missing_slots")

    def choose_execution_strategy(
        self,
        request: OrchestratorRequest,
        routed: RoutedDecision,
        *,
        mode: ExecutionMode,
        llm_config: ResolvedLLMConfig,
    ) -> ExecutionStrategy:
        if routed.final_intent in FIXED_WORKFLOW_INTENTS:
            if routed.confidence < 0.75:
                raise FixedWorkflowEntryError("fixed_workflow.low_confidence")
            return ExecutionStrategy(kind="fixed_workflow", reason="Final intent is a fixed workflow.", confidence=routed.confidence)

        if routed.final_intent != "open_analytics_query":
            raise FixedWorkflowEntryError("fixed_workflow.unsupported_entry")

        if self._should_use_agent_planned_strategy(request, routed):
            if mode == "agents_sdk" and llm_config.can_use_agents_sdk:
                return ExecutionStrategy(kind="agent_planned", reason="Open analytics question is eligible for controlled agent runtime.", confidence=routed.confidence)
            return ExecutionStrategy(
                kind="template_analytics",
                reason="Agent-runtime-eligible analytics, but agents_sdk is unavailable in the resolved execution mode; using template analytics.",
                confidence=routed.confidence,
            )

        return ExecutionStrategy(kind="template_analytics", reason="Supported standard open analytics template.", confidence=routed.confidence)

    def _should_use_agent_planned_strategy(self, request: OrchestratorRequest, routed: RoutedDecision) -> bool:
        subtype = routed.final_subtype
        scope = routed.final_scope
        question = request.raw_text or ""
        lowered = question.lower()

        if subtype in {"absent_from_baseline_window", "doctors_with_active_plans"}:
            return True
        if scope == "doctor_aggregate":
            return True
        if subtype == "absent_old_patients_recent_window":
            return self._has_complex_analytics_signal(question)
        if subtype is None:
            return any(
                token in question or token in lowered
                for token in (
                    "compare",
                    "baseline",
                    "aggregate",
                    "which doctors",
                    "哪些医生",
                    "各医生",
                    "全院",
                    "统计",
                    "比较",
                    "排除",
                    "基线",
                )
            )
        return self._has_complex_analytics_signal(question)

    def _has_complex_analytics_signal(self, question: str) -> bool:
        lowered = question.lower()
        if any(token in question or token in lowered for token in ("compare", "baseline", "排除", "统计", "比较", "基线", "前一阶段")):
            return True
        return bool(
            re.search(r"\d+\s*[-到至]\s*\d+\s*(天|days?)", question, flags=re.IGNORECASE)
            or re.search(r"(past|last)\s*\d+\s*days?.*(exclude|except).*\d+\s*days?", lowered)
        )

    def _build_strategy_trace(self, strategy: ExecutionStrategy) -> StepExecutionResult:
        return StepExecutionResult(
            step_id="choose_execution_strategy",
            tool_name="strategy_chooser",
            success=True,
            args={},
            output_summary=f"kind={strategy.kind}; confidence={strategy.confidence}",
            raw_output=strategy.model_dump(mode="json"),
        )

    def _refine_intent_with_llm_if_needed(
        self,
        *,
        request: OrchestratorRequest,
        rule_decision,
        llm_config: ResolvedLLMConfig,
        mode: ExecutionMode,
    ) -> RoutedDecision:
        should_refine = self.llm_router.should_refine(request, rule_decision)
        llm_decision = None
        if should_refine:
            llm_decision = self.llm_router.refine(
                request,
                rule_decision,
                llm_config=llm_config,
                mode=mode,
            )
        routed = merge_rule_and_llm(rule_decision, llm_decision)
        return routed

    def _build_route_trace(self, routed: RoutedDecision) -> StepExecutionResult:
        llm_called = routed.llm_decision is not None
        return StepExecutionResult(
            step_id="route_intent",
            tool_name="llm_router" if llm_called else "rule_router",
            success=True,
            args={},
            output_summary=(
                f"final_intent={routed.final_intent}; "
                f"final_subtype={routed.final_subtype or 'none'}; "
                f"final_scope={routed.final_scope or 'none'}; "
                f"lookup={routed.lookup_entity_type or 'none'}:{routed.lookup_user_id or 'none'}; "
                f"result_set={routed.result_set_operation or 'none'}:{routed.result_set_filter_kind or routed.result_set_target_field or 'none'}; "
                f"llm_refined={llm_called}"
            ),
            raw_output=routed.model_dump(mode="json"),
        )

    def route_intent(self, request: OrchestratorRequest) -> OrchestrationIntent:
        raw_query = (request.raw_text or "").strip()
        context = dict(request.context or {})
        normalized_task = normalize_task_type(request.task_type)
        extracted = self._extract_slots(raw_query)
        identity = request.identity_context

        if normalized_task == OrchestrationTaskType.UNKNOWN:
            normalized_task = self._infer_task_type(raw_query, context)

        # Server-side priority: identity_context -> explicit request fields -> text targets -> loose conversation context.
        # Demo defaults are intentionally excluded from the production orchestration path.
        identity_patient_id = identity.effective_patient_id if identity else None
        identity_doctor_id = identity.actor_doctor_id if identity and identity.actor_role == "doctor" else None
        patient_id = self._coalesce(identity_patient_id, request.patient_id, extracted.get("patient_id"), context.get("patient_id"))
        plan_id = self._coalesce(request.plan_id, extracted.get("plan_id"), context.get("plan_id"))
        therapist_id = self._coalesce(identity_doctor_id, request.therapist_id, request.doctor_id, extracted.get("therapist_id"), context.get("therapist_id"))
        days = self._coalesce(request.days, extracted.get("days"), context.get("days"))
        top_k = self._coalesce(request.top_k, extracted.get("top_k"), context.get("top_k"), 10)

        if normalized_task in {OrchestrationTaskType.SCREEN_RISK, OrchestrationTaskType.WEEKLY_REPORT}:
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
        if request.use_agent_sdk is False:
            return "direct", "direct", []
        requested_agent_sdk = request.use_agent_sdk is True
        if llm_config.can_use_agents_sdk:
            return "agents_sdk", "agents_sdk", []
        if requested_agent_sdk and not llm_config.can_use_agents_sdk:
            return "direct", "direct_fallback", ["execution_mode.fallback_to_direct"]
        return "direct", "direct_fallback", ["execution_mode.fallback_to_direct"]

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

    def _extract_top_k(self, text: str) -> int | None:
        return extract_limit(text)

    def _extract_response_style(self, text: str) -> str | None:
        lowered = text.lower()
        if any(keyword in text or keyword in lowered for keyword in DETAIL_KEYWORDS):
            return "detailed"
        if any(keyword in text or keyword in lowered for keyword in BRIEF_KEYWORDS):
            return "brief"
        return None

    def _extract_identifier(self, text: str, labels: tuple[str, ...]) -> int | None:
        expanded_labels = list(labels)
        if any(label in labels for label in ("doctor", "therapist")):
            expanded_labels.extend(["医生", "治疗师", "康复师"])
        if any(label in labels for label in ("patient",)):
            expanded_labels.extend(["患者", "病人"])
        if any(label in labels for label in ("plan",)):
            expanded_labels.append("计划")
        for label in expanded_labels:
            pattern = rf"{re.escape(label)}\s*(?:id)?\s*[:：]?\s*(\d+)"
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    def _extract_days(self, text: str) -> int | None:
        lowered = text.lower()
        if any(token in text for token in ("本周", "最近一周", "近一周")) or "last week" in lowered:
            return 7
        if any(token in text for token in ("本月", "最近一个月", "近一个月")) or "last month" in lowered:
            return 30
        for pattern in (
            r"(?:最近|过去|近|当前)\s*(\d+)\s*天",
            r"(\d+)\s*天",
            r"last\s*(\d+)\s*days?",
        ):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    def _extract_slots(self, text: str) -> dict[str, Any]:
        if not text:
            return {}
        return {
            "patient_id": self._extract_identifier(text, ("患者", "病人", "patient")),
            "plan_id": self._extract_identifier(text, ("计划", "plan")),
            "therapist_id": self._extract_identifier(text, ("医生", "治疗师", "康复师", "doctor", "therapist")),
            "days": self._extract_days(text),
            "top_k": self._extract_top_k(text),
            "need_gait_evidence": any(keyword in text.lower() for keyword in ("gait", "walkway", "walk")),
            "response_style": self._extract_response_style(text),
        }

    def _is_follow_up(self, text: str) -> bool:
        lowered = text.lower()
        return any(keyword in text or keyword in lowered for keyword in FOLLOW_UP_KEYWORDS)

    def _coalesce(self, *values: Any) -> Any:
        for value in values:
            if value is not None:
                return value
        return None
