from __future__ import annotations

import logging
import re
from datetime import datetime, time, timedelta
from typing import Any

from config import ResolvedLLMConfig, Settings, get_settings
from models import DoctorAnalyticsResultRow, PatientSet, RankedPatients, TimeRange
from services import AnalyticsService
from services.shared import build_time_range, parse_datetime_flexible, resolve_time_anchor
from tools import ToolSpec

from .llm_planner import LLMPlanner
from .open_analytics_agent import OpenAnalyticsAgentRuntime
from .plan_validator import PlanValidator
from .schemas import (
    AgentAnalyticsResult,
    AnalyticsScope,
    AnalyticsStructuredOutput,
    AnalyticsTimeSlots,
    IntentDecision,
    ExecutionStrategy,
    LLMPlannedQuery,
    LLMPlannedStep,
    OpenAnalyticsSubtype,
    OrchestratorRequest,
    OrchestratorResponse,
    PlannedQuerySource,
    PlanValidationResult,
    QueryPlan,
    QueryPlanStep,
    RelativeWindow,
    ResolvedAnalyticsRanges,
    ResolvedWindow,
    RoutedDecision,
    StepExecutionResult,
)


logger = logging.getLogger(__name__)

SUPPORTED_ANALYTICS_SUBTYPES: tuple[OpenAnalyticsSubtype, ...] = (
    "absent_old_patients_recent_window",
    "absent_from_baseline_window",
    "doctors_with_active_plans",
)
PATIENT_ANALYSIS_SUBTYPES = {
    "absent_old_patients_recent_window",
    "absent_from_baseline_window",
}


class AnalyticsManager:
    def __init__(
        self,
        analytics_service: AnalyticsService,
        analytics_tool_registry: dict[str, ToolSpec],
        settings: Settings | None = None,
        llm_planner: LLMPlanner | None = None,
        plan_validator: PlanValidator | None = None,
        agent_runtime: OpenAnalyticsAgentRuntime | None = None,
    ):
        self.analytics_service = analytics_service
        self.analytics_tool_registry = analytics_tool_registry
        self.settings = settings or get_settings()
        self.llm_planner = llm_planner or LLMPlanner(settings=self.settings)
        self.plan_validator = plan_validator or PlanValidator(tool_registry=self.analytics_tool_registry)
        self.agent_runtime = agent_runtime or OpenAnalyticsAgentRuntime(settings=self.settings)

    def run(
        self,
        *,
        request: OrchestratorRequest,
        routed_decision: RoutedDecision,
        strategy: ExecutionStrategy,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
    ) -> OrchestratorResponse:
        if strategy.kind == "agent_planned":
            return self._run_agent_planned(
                request=request,
                routed_decision=routed_decision,
                mode=mode,
                llm_config=llm_config,
                execution_mode=execution_mode,
            )
        if strategy.kind == "template_analytics":
            return self._run_template(
                request,
                routed_decision,
                mode=mode,
                llm_config=llm_config,
                execution_mode=execution_mode,
            )
        return self._build_not_supported_response(
            request,
            self._effective_intent_decision(routed_decision),
            llm_config=llm_config,
            execution_mode=execution_mode,
            reason=f"Analytics manager received non-analytics strategy: {strategy.kind}.",
            planned_query_source=PlannedQuerySource(source="fixed_template", note=strategy.reason),
        )

    def _run_template(
        self,
        request: OrchestratorRequest,
        decision: IntentDecision | RoutedDecision,
        *,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        source_note: str | None = None,
    ) -> OrchestratorResponse:
        question = (request.raw_text or "").strip()
        effective_decision = self._effective_intent_decision(decision)
        subtype = effective_decision.analytics_subtype
        logger.info(
            "open analytics execute subtype=%s scope=%s question=%r",
            subtype,
            effective_decision.analysis_scope,
            question,
        )

        if subtype == "absent_old_patients_recent_window":
            return self._run_absent_old_patients_recent_window(
                request,
                effective_decision,
                mode=mode,
                llm_config=llm_config,
                execution_mode=execution_mode,
                planned_query_source=PlannedQuerySource(source="fixed_template", note=source_note),
            )
        if subtype == "absent_from_baseline_window":
            return self._run_absent_from_baseline_window(
                request,
                effective_decision,
                mode=mode,
                llm_config=llm_config,
                execution_mode=execution_mode,
                planned_query_source=PlannedQuerySource(source="fixed_template", note=source_note),
            )
        if subtype == "doctors_with_active_plans":
            return self._run_doctors_with_active_plans(
                request,
                effective_decision,
                mode=mode,
                llm_config=llm_config,
                execution_mode=execution_mode,
                planned_query_source=PlannedQuerySource(source="fixed_template", note=source_note),
            )
        return self._build_not_supported_response(
            request,
            effective_decision,
            llm_config=llm_config,
            execution_mode=execution_mode,
            reason="Unable to stably classify this open analytics question into a supported subtype.",
            planned_query_source=PlannedQuerySource(source="fixed_template", note=source_note),
        )

    def _run_agent_planned(
        self,
        *,
        request: OrchestratorRequest,
        routed_decision: RoutedDecision,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
    ) -> OrchestratorResponse:
        prefix_trace: list[StepExecutionResult] = []
        runtime_issue: str | None = None
        if self.agent_runtime.can_run(mode=mode, llm_config=llm_config):
            tool_specs = self._agent_tool_specs(routed_decision)
            try:
                agent_request = self._request_with_agent_runtime_context(request, routed_decision)
                agent_result = self.agent_runtime.run(
                    request=agent_request,
                    routed_decision=routed_decision,
                    tool_specs=tool_specs,
                    llm_config=llm_config,
                )
                return self._response_from_agent_result(
                    agent_result,
                    request=request,
                    routed_decision=routed_decision,
                    llm_config=llm_config,
                    execution_mode=execution_mode,
                )
            except Exception as exc:  # noqa: BLE001
                runtime_issue = f"agents_sdk_runtime.fallback:{type(exc).__name__}:{exc}"
                logger.warning("open analytics agent runtime falling back to planner: %s", exc)
                prefix_trace.append(
                    StepExecutionResult(
                        step_id="agents_sdk_runtime",
                        tool_name="open_analytics_agent",
                        success=False,
                        args={"tool_count": len(tool_specs)},
                        output_summary="agent runtime failed; fallback planner will run",
                        raw_output=None,
                        error=str(exc),
                    )
                )
        else:
            prefix_trace.append(
                StepExecutionResult(
                    step_id="agents_sdk_runtime",
                    tool_name="open_analytics_agent",
                    success=False,
                    args={"mode": mode},
                    output_summary="agent runtime unavailable; fallback planner will run",
                    raw_output=None,
                    error="agents_sdk_runtime.unavailable",
                )
            )

        response = self._run_via_llm_planner(
            request=request,
            routed_decision=routed_decision,
            mode=mode,
            llm_config=llm_config,
            execution_mode=execution_mode,
            prefix_trace=prefix_trace,
        )
        if runtime_issue:
            response.validation_issues = [runtime_issue] + list(response.validation_issues)
        return response

    def _request_with_agent_runtime_context(
        self,
        request: OrchestratorRequest,
        routed_decision: RoutedDecision,
    ) -> OrchestratorRequest:
        analysis_scope: AnalyticsScope = routed_decision.final_scope or "single_doctor"
        doctor_id, explicit_doctor = self._resolve_doctor_context(request, analysis_scope=analysis_scope)
        time_slots = self._extract_time_slots((request.raw_text or "").strip(), request)
        resolved_ranges = self._resolve_time_slots(
            time_slots,
            doctor_id=doctor_id if analysis_scope != "doctor_aggregate" else None,
            patient_id=request.patient_id,
        )
        recent_window = resolved_ranges.recent_window
        baseline_window = resolved_ranges.baseline_window
        context = dict(request.context or {})
        context["agent_runtime_context"] = {
            "analysis_scope": analysis_scope,
            "doctor_id": doctor_id if analysis_scope != "doctor_aggregate" else None,
            "explicit_doctor": explicit_doctor,
            "time_slots": time_slots.model_dump(mode="json"),
            "resolved_ranges": resolved_ranges.model_dump(mode="json"),
            "tool_date_arguments": {
                "recent_start_date": self._date_portion(recent_window.start) if recent_window else None,
                "recent_end_date": self._date_portion(recent_window.end) if recent_window else None,
                "baseline_start_date": self._date_portion(baseline_window.start) if baseline_window else None,
                "baseline_end_date": self._date_portion(baseline_window.end) if baseline_window else None,
            },
        }
        update: dict[str, Any] = {
            "context": context,
            "analytics_time_slots": time_slots,
        }
        if analysis_scope != "doctor_aggregate" and doctor_id is not None and request.therapist_id is None:
            update["therapist_id"] = doctor_id
        return request.model_copy(update=update)

    def _run_via_llm_planner(
        self,
        *,
        request: OrchestratorRequest,
        routed_decision: RoutedDecision,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        prefix_trace: list[StepExecutionResult] | None = None,
    ) -> OrchestratorResponse:
        planner_trace: list[StepExecutionResult] = list(prefix_trace or [])
        fallback_note: str | None = None
        try:
            tool_catalog = self._tool_catalog(routed_decision)
            planned = self.llm_planner.plan(
                request=request,
                routed_decision=routed_decision,
                tool_catalog=tool_catalog,
                llm_config=llm_config,
                mode=mode,
            )
            normalized_plan, query_plan, explicit_doctor = self._normalize_llm_plan(
                request=request,
                routed_decision=routed_decision,
                planned=planned,
            )
            planner_trace.append(
                StepExecutionResult(
                    step_id="llm_plan",
                    tool_name="llm_planner",
                    success=True,
                    args={"tool_catalog_size": len(tool_catalog)},
                    output_summary=f"planner returned {len(normalized_plan.steps)} steps",
                    raw_output=normalized_plan.model_dump(mode="json"),
                )
            )
            validation = self.plan_validator.validate(normalized_plan, routed_decision=routed_decision)
            planner_trace.append(
                StepExecutionResult(
                    step_id="validate_plan",
                    tool_name="plan_validator",
                    success=validation.is_valid,
                    args={},
                    output_summary="plan valid" if validation.is_valid else f"plan invalid issues={len(validation.issues)}",
                    raw_output=validation.model_dump(mode="json"),
                    error=None if validation.is_valid else "; ".join(issue.code for issue in validation.issues),
                )
            )
            if not validation.is_valid:
                fallback_note = "plan_validation_failed:" + ",".join(issue.code for issue in validation.issues)
                return self._fallback_to_template(
                    request=request,
                    routed_decision=routed_decision,
                    mode=mode,
                    llm_config=llm_config,
                    execution_mode=execution_mode,
                    fallback_note=fallback_note,
                    prefix_trace=planner_trace,
                    validation=validation,
                )

            response = self._execute_query_plan(
                question=(request.raw_text or "").strip(),
                request=request,
                mode=mode,
                llm_config=llm_config,
                execution_mode=execution_mode,
                query_plan=query_plan,
                explicit_doctor=explicit_doctor,
                planned_query_source=PlannedQuerySource(source="llm_planner", note=normalized_plan.rationale),
                strict_failures=True,
            )
            response.execution_trace = planner_trace + list(response.execution_trace)
            return response
        except Exception as exc:  # noqa: BLE001
            fallback_note = str(exc)
            logger.warning("llm planner branch falling back to template: %s", fallback_note)
            failure_step_id = "execute_llm_plan" if any(item.step_id == "llm_plan" and item.success for item in planner_trace) else "llm_plan"
            planner_trace.append(
                StepExecutionResult(
                    step_id=failure_step_id,
                    tool_name="llm_planner",
                    success=False,
                    args={},
                    output_summary="planner branch failed; fallback template will run",
                    raw_output=None,
                    error=fallback_note,
                )
            )
            return self._fallback_to_template(
                request=request,
                routed_decision=routed_decision,
                mode=mode,
                llm_config=llm_config,
                execution_mode=execution_mode,
                fallback_note=fallback_note,
                prefix_trace=planner_trace,
            )

    def _response_from_agent_result(
        self,
        result: AgentAnalyticsResult,
        *,
        request: OrchestratorRequest,
        routed_decision: RoutedDecision,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
    ) -> OrchestratorResponse:
        source = PlannedQuerySource(source="agents_sdk_runtime", note=result.rationale)
        structured_output = dict(result.structured_output or {})
        structured_output.setdefault("question", request.raw_text or result.normalized_question)
        structured_output.setdefault("subtype", result.subtype or routed_decision.final_subtype)
        structured_output.setdefault("analysis_scope", result.scope or routed_decision.final_scope)
        structured_output.setdefault("summary", result.final_text)
        structured_output["planned_query_source"] = source.model_dump(mode="json")
        if "query_plan" not in structured_output:
            structured_output["query_plan"] = QueryPlan(
                normalized_question=result.normalized_question,
                subtype=result.subtype or routed_decision.final_subtype,
                analysis_scope=result.scope or routed_decision.final_scope,
                steps=[
                    QueryPlanStep(
                        step_id=f"agent_tool_{index}",
                        intent="agents_sdk_runtime_tool_call",
                        tool_name=call.tool_name,
                        arguments=call.arguments,
                        rationale="Called by the Agents SDK runtime.",
                    )
                    for index, call in enumerate(result.tool_calls, start=1)
                ],
            ).model_dump(mode="json")
        structured_output["agent_runtime"] = {
            "source": result.source,
            "tool_call_count": len(result.tool_calls),
        }

        trace = [
            StepExecutionResult(
                step_id="agents_sdk_runtime",
                tool_name="open_analytics_agent",
                success=True,
                args={"tool_call_count": len(result.tool_calls)},
                output_summary=f"agent runtime completed with {len(result.tool_calls)} tool calls",
                raw_output=result.model_dump(mode="json"),
            )
        ]
        trace.extend(
            StepExecutionResult(
                step_id=f"agent_tool_{index}_{call.tool_name}",
                tool_name=call.tool_name,
                success=True,
                args=call.arguments,
                output_summary=call.output_summary or "tool called by agent runtime",
                raw_output=None,
            )
            for index, call in enumerate(result.tool_calls, start=1)
        )
        validation_issues: list[str] = []
        if not result.final_text.strip():
            validation_issues.append("agents_sdk_runtime.empty_final_text")
        return OrchestratorResponse(
            success=not validation_issues,
            task_type="open_analytics_query",
            execution_mode=execution_mode,
            llm_provider=llm_config.provider,
            llm_model=llm_config.model,
            structured_output=structured_output,
            final_text=result.final_text,
            validation_issues=validation_issues,
            execution_trace=trace,
        )

    def _effective_intent_decision(self, decision: IntentDecision | RoutedDecision) -> IntentDecision:
        if isinstance(decision, IntentDecision):
            return decision
        return IntentDecision(
            intent=decision.final_intent,
            confidence=decision.confidence,
            rationale=decision.rationale,
            analytics_subtype=decision.final_subtype,
            analysis_scope=decision.final_scope,
            doctor_id_source=decision.doctor_id_source,
        )

    def _fallback_to_template(
        self,
        *,
        request: OrchestratorRequest,
        routed_decision: RoutedDecision,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        fallback_note: str,
        prefix_trace: list[StepExecutionResult],
        validation: PlanValidationResult | None = None,
    ) -> OrchestratorResponse:
        response = self._run_template(
            request,
            routed_decision,
            mode=mode,
            llm_config=llm_config,
            execution_mode=execution_mode,
            source_note=fallback_note,
        )
        self._mark_response_source(
            response,
            PlannedQuerySource(source="fallback_template", note=fallback_note),
        )
        response.execution_trace = prefix_trace + list(response.execution_trace)
        issue_codes = [f"llm_planner.fallback:{fallback_note}"]
        if validation is not None:
            issue_codes.extend(f"plan_validator.{issue.code}" for issue in validation.issues)
        response.validation_issues = issue_codes + list(response.validation_issues)
        return response

    def _mark_response_source(self, response: OrchestratorResponse, source: PlannedQuerySource) -> None:
        if isinstance(response.structured_output, dict):
            response.structured_output["planned_query_source"] = source.model_dump(mode="json")

    def _tool_catalog(self, routed_decision: RoutedDecision) -> list[dict[str, Any]]:
        scope = routed_decision.final_scope
        if scope == "doctor_aggregate":
            allowed_names = {"list_doctors_with_active_plans"}
        else:
            allowed_names = {
                "list_patients_seen_by_doctor",
                "list_patients_with_active_plans",
                "set_diff",
                "get_patient_last_visit",
                "get_patient_plan_status",
                "rank_patients",
            }
        catalog: list[dict[str, Any]] = []
        for tool_name in allowed_names:
            tool = self.analytics_tool_registry.get(tool_name)
            if tool is None:
                continue
            metadata = tool.metadata()
            notes: list[str] = []
            if tool_name in {"list_patients_seen_by_doctor", "list_patients_with_active_plans"}:
                notes.append("single_doctor scoped; include doctor_id")
            if tool_name == "list_doctors_with_active_plans":
                notes.append("doctor_aggregate scoped; do not pass doctor_id")
            if tool_name == "set_diff":
                notes.append("may use base_set_ref and subtract_set_ref to refer to previous patient-set steps")
            if tool_name in {"get_patient_last_visit", "get_patient_plan_status", "rank_patients"}:
                notes.append("may use patient_set_ref or patient_ids_ref to fan out from a previous patient-set step")
            catalog.append(
                {
                    "tool_name": metadata["tool_name"],
                    "description": metadata["description"],
                    "input_schema": metadata["input_schema"],
                    "chain_scope": metadata["chain_scope"],
                    "notes": notes,
                }
            )
        return sorted(catalog, key=lambda item: item["tool_name"])

    def _agent_tool_specs(self, routed_decision: RoutedDecision) -> list[ToolSpec]:
        if routed_decision.final_scope == "doctor_aggregate":
            allowed_names = {"list_doctors_with_active_plans"}
        else:
            allowed_names = {
                "list_patients_seen_by_doctor",
                "list_patients_with_active_plans",
                "set_diff",
                "rank_patients",
            }
        tool_specs: list[ToolSpec] = []
        for tool_name in sorted(allowed_names):
            tool = self.analytics_tool_registry.get(tool_name)
            if tool is not None and tool.get_agent_tool() is not None:
                tool_specs.append(tool)
        return tool_specs

    def _normalize_llm_plan(
        self,
        *,
        request: OrchestratorRequest,
        routed_decision: RoutedDecision,
        planned: LLMPlannedQuery,
    ) -> tuple[LLMPlannedQuery, QueryPlan, bool]:
        scope = planned.scope or routed_decision.final_scope
        subtype = planned.subtype or routed_decision.final_subtype
        analysis_scope: AnalyticsScope = scope or "single_doctor"
        doctor_id, explicit_doctor = self._resolve_doctor_context(request, analysis_scope=analysis_scope)
        time_slots = self._extract_time_slots((request.raw_text or "").strip(), request)
        resolved_ranges = self._resolve_time_slots(
            time_slots,
            doctor_id=doctor_id if analysis_scope != "doctor_aggregate" else None,
            patient_id=request.patient_id,
        )
        token_values = self._planner_token_values(
            doctor_id=doctor_id,
            request=request,
            resolved_ranges=resolved_ranges,
        )
        normalized_steps: list[LLMPlannedStep] = []
        for step in planned.steps:
            args = self._replace_planner_tokens(step.arguments or {}, token_values)
            if analysis_scope == "single_doctor" and step.tool_name in {"list_patients_seen_by_doctor", "list_patients_with_active_plans"}:
                args.setdefault("doctor_id", doctor_id)
            args = self._enforce_planned_window_args(
                step=step,
                args=args,
                subtype=subtype,
                token_values=token_values,
            )
            if step.tool_name == "rank_patients":
                strategy = str(args.get("strategy") or "").strip().lower()
                if strategy in {"last_visit", "oldest_last_visit", "last_visit_time"} or ("last" in strategy and "visit" in strategy):
                    args["strategy"] = "last_visit_oldest"
                elif strategy and strategy not in {"active_plan_but_absent", "last_visit_oldest", "highest_risk"}:
                    args["strategy"] = "active_plan_but_absent"
                args.setdefault("strategy", "active_plan_but_absent")
            normalized_steps.append(
                LLMPlannedStep(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    arguments=args,
                    rationale=step.rationale,
                )
            )
        normalized_plan = planned.model_copy(
            update={
                "subtype": subtype,
                "scope": analysis_scope,
                "steps": normalized_steps,
            }
        )
        query_steps = [
            QueryPlanStep(
                step_id=step.step_id,
                intent="llm_planner",
                tool_name=step.tool_name,
                arguments=step.arguments,
                rationale=step.rationale,
            )
            for step in normalized_plan.steps
        ]
        query_plan = self._build_query_plan(
            normalized_question=normalized_plan.normalized_question,
            subtype=subtype,
            analysis_scope=analysis_scope,
            doctor_id=doctor_id,
            time_slots=time_slots,
            resolved_ranges=resolved_ranges,
            steps=query_steps,
        )
        return normalized_plan, query_plan, explicit_doctor

    def _enforce_planned_window_args(
        self,
        *,
        step: LLMPlannedStep,
        args: dict[str, Any],
        subtype: OpenAnalyticsSubtype | None,
        token_values: dict[str, Any],
    ) -> dict[str, Any]:
        enforced = dict(args)
        text = f"{step.step_id} {step.rationale}".lower()
        if step.tool_name == "list_patients_seen_by_doctor":
            if subtype == "absent_from_baseline_window":
                if any(token in text for token in ("baseline", "historical", "history", "prior", "old", "base")):
                    self._apply_window(enforced, token_values.get("BASELINE_START"), token_values.get("BASELINE_END"))
                elif "recent" in text:
                    self._apply_window(enforced, token_values.get("RECENT_START"), token_values.get("RECENT_END"))
            elif subtype == "absent_old_patients_recent_window":
                if any(token in text for token in ("historical", "history", "prior", "old", "baseline", "base")):
                    self._apply_window(enforced, None, token_values.get("HISTORICAL_END"))
                elif "recent" in text:
                    self._apply_window(enforced, token_values.get("RECENT_START"), token_values.get("RECENT_END"))
        if step.tool_name == "list_patients_with_active_plans":
            self._apply_window(enforced, token_values.get("RECENT_START"), token_values.get("RECENT_END"))
        if step.tool_name == "get_patient_plan_status":
            self._apply_window(enforced, token_values.get("RECENT_START"), token_values.get("RECENT_END"))
        return enforced

    def _apply_window(self, args: dict[str, Any], start_date: Any, end_date: Any) -> None:
        if start_date is not None:
            args["start_date"] = start_date
        if end_date is not None:
            args["end_date"] = end_date

    def _planner_token_values(
        self,
        *,
        doctor_id: int | None,
        request: OrchestratorRequest,
        resolved_ranges: ResolvedAnalyticsRanges,
    ) -> dict[str, Any]:
        recent = resolved_ranges.recent_window
        baseline = resolved_ranges.baseline_window
        historical_end = None
        if recent is not None:
            historical_end = self._date_portion_from_datetime(
                self._parse_required_datetime(recent.start) - timedelta(seconds=1)
            )
        return {
            "DOCTOR_ID": doctor_id,
            "THERAPIST_ID": doctor_id,
            "TOP_K": request.top_k or 20,
            "RECENT_START": self._date_portion(recent.start) if recent else None,
            "RECENT_END": self._date_portion(recent.end) if recent else None,
            "BASELINE_START": self._date_portion(baseline.start) if baseline else None,
            "BASELINE_END": self._date_portion(baseline.end) if baseline else None,
            "HISTORICAL_END": historical_end,
        }

    def _replace_planner_tokens(self, value: Any, token_values: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {key: self._replace_planner_tokens(item, token_values) for key, item in value.items()}
        if isinstance(value, list):
            return [self._replace_planner_tokens(item, token_values) for item in value]
        if isinstance(value, str):
            token = value.strip()
            upper_token = token.upper()
            if upper_token in token_values:
                replacement = token_values[upper_token]
                if replacement is None:
                    raise ValueError(f"planner_unresolved_placeholder:{token}")
                return replacement
        return value

    def _run_absent_old_patients_recent_window(
        self,
        request: OrchestratorRequest,
        decision: IntentDecision,
        *,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        planned_query_source: PlannedQuerySource | None = None,
    ) -> OrchestratorResponse:
        question = (request.raw_text or "").strip()
        doctor_id, explicit_doctor = self._resolve_doctor_context(request, analysis_scope="single_doctor")
        time_slots = self._extract_time_slots(question, request)
        resolved_ranges = self._resolve_time_slots(time_slots, doctor_id=doctor_id, patient_id=request.patient_id)
        recent_window = resolved_ranges.recent_window
        if recent_window is None or doctor_id is None:
            return self._build_not_supported_response(
                request,
                decision,
                llm_config=llm_config,
                execution_mode=execution_mode,
                reason="This subtype needs a single doctor scope and a resolvable recent window.",
                time_slots=time_slots,
                resolved_ranges=resolved_ranges,
            )

        recent_start_date = self._date_portion(recent_window.start)
        recent_end_date = self._date_portion(recent_window.end)
        historical_end_date = self._date_portion_from_datetime(
            self._parse_required_datetime(recent_window.start) - timedelta(seconds=1)
        )
        top_k = request.top_k or 20

        query_plan = self._build_query_plan(
            normalized_question=f"Find patients for doctor {doctor_id} who visited historically but not in the recent window.",
            subtype="absent_old_patients_recent_window",
            analysis_scope="single_doctor",
            doctor_id=doctor_id,
            time_slots=time_slots,
            resolved_ranges=resolved_ranges,
            steps=[
                QueryPlanStep(
                    step_id="step_1",
                    intent="historical_seen_cohort",
                    tool_name="list_patients_seen_by_doctor",
                    arguments={"doctor_id": doctor_id, "start_date": None, "end_date": historical_end_date, "source": "attendance"},
                    rationale="Collect the historical cohort before the recent window.",
                ),
                QueryPlanStep(
                    step_id="step_2",
                    intent="recent_seen_cohort",
                    tool_name="list_patients_seen_by_doctor",
                    arguments={"doctor_id": doctor_id, "start_date": recent_start_date, "end_date": recent_end_date, "source": "attendance"},
                    rationale="Collect the patients seen in the recent window.",
                ),
                QueryPlanStep(
                    step_id="step_3",
                    intent="recent_active_plans",
                    tool_name="list_patients_with_active_plans",
                    arguments={"doctor_id": doctor_id, "start_date": recent_start_date, "end_date": recent_end_date},
                    rationale="Collect recent active plans to prioritize absent patients.",
                ),
                QueryPlanStep(step_id="step_4", intent="absent_candidate_diff", tool_name="set_diff", arguments={}, rationale="Subtract recent attendees from the historical cohort."),
                QueryPlanStep(step_id="step_5", intent="last_visit_enrichment", tool_name="get_patient_last_visit", arguments={"doctor_id": doctor_id}, rationale="Attach the latest visit for each absent patient."),
                QueryPlanStep(step_id="step_6", intent="plan_status_enrichment", tool_name="get_patient_plan_status", arguments={"doctor_id": doctor_id, "start_date": recent_start_date, "end_date": recent_end_date}, rationale="Attach plan status inside the recent window."),
                QueryPlanStep(step_id="step_7", intent="ranking", tool_name="rank_patients", arguments={"strategy": "active_plan_but_absent", "top_k": top_k}, rationale="Rank absent patients, preferring active-plan absences."),
            ],
        )
        return self._execute_absent_patient_analysis(
            question=question,
            request=request,
            mode=mode,
            llm_config=llm_config,
            execution_mode=execution_mode,
            query_plan=query_plan,
            doctor_id=doctor_id,
            explicit_doctor=explicit_doctor,
            time_slots=time_slots,
            resolved_ranges=resolved_ranges,
            planned_query_source=planned_query_source or PlannedQuerySource(source="fixed_template"),
        )

    def _run_absent_from_baseline_window(
        self,
        request: OrchestratorRequest,
        decision: IntentDecision,
        *,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        planned_query_source: PlannedQuerySource | None = None,
    ) -> OrchestratorResponse:
        question = (request.raw_text or "").strip()
        doctor_id, explicit_doctor = self._resolve_doctor_context(request, analysis_scope="single_doctor")
        time_slots = self._extract_time_slots(question, request)
        resolved_ranges = self._resolve_time_slots(time_slots, doctor_id=doctor_id, patient_id=request.patient_id)
        baseline_window = resolved_ranges.baseline_window
        recent_window = resolved_ranges.recent_window
        if baseline_window is None or recent_window is None or doctor_id is None:
            return self._build_not_supported_response(
                request,
                decision,
                llm_config=llm_config,
                execution_mode=execution_mode,
                reason="This subtype needs both baseline and recent windows plus a single doctor scope.",
                time_slots=time_slots,
                resolved_ranges=resolved_ranges,
            )

        recent_start_date = self._date_portion(recent_window.start)
        recent_end_date = self._date_portion(recent_window.end)
        baseline_start_date = self._date_portion(baseline_window.start)
        baseline_end_date = self._date_portion(baseline_window.end)
        top_k = request.top_k or 20

        query_plan = self._build_query_plan(
            normalized_question=f"Compare baseline and recent attendance for doctor {doctor_id} and return baseline-only patients.",
            subtype="absent_from_baseline_window",
            analysis_scope="single_doctor",
            doctor_id=doctor_id,
            time_slots=time_slots,
            resolved_ranges=resolved_ranges,
            steps=[
                QueryPlanStep(step_id="step_1", intent="baseline_seen_cohort", tool_name="list_patients_seen_by_doctor", arguments={"doctor_id": doctor_id, "start_date": baseline_start_date, "end_date": baseline_end_date, "source": "attendance"}, rationale="Collect patients seen in the baseline window."),
                QueryPlanStep(step_id="step_2", intent="recent_seen_cohort", tool_name="list_patients_seen_by_doctor", arguments={"doctor_id": doctor_id, "start_date": recent_start_date, "end_date": recent_end_date, "source": "attendance"}, rationale="Collect patients seen in the recent window."),
                QueryPlanStep(step_id="step_3", intent="recent_active_plans", tool_name="list_patients_with_active_plans", arguments={"doctor_id": doctor_id, "start_date": recent_start_date, "end_date": recent_end_date}, rationale="Collect recent active plans to prioritize baseline-only absences."),
                QueryPlanStep(step_id="step_4", intent="baseline_not_recent_diff", tool_name="set_diff", arguments={}, rationale="Subtract recent attendees from the baseline cohort."),
                QueryPlanStep(step_id="step_5", intent="last_visit_enrichment", tool_name="get_patient_last_visit", arguments={"doctor_id": doctor_id}, rationale="Attach the latest visit for each absent patient."),
                QueryPlanStep(step_id="step_6", intent="plan_status_enrichment", tool_name="get_patient_plan_status", arguments={"doctor_id": doctor_id, "start_date": recent_start_date, "end_date": recent_end_date}, rationale="Attach plan status in the recent window."),
                QueryPlanStep(step_id="step_7", intent="ranking", tool_name="rank_patients", arguments={"strategy": "active_plan_but_absent", "top_k": top_k}, rationale="Rank baseline-only absences, preferring active-plan cases."),
            ],
        )
        return self._execute_absent_patient_analysis(
            question=question,
            request=request,
            mode=mode,
            llm_config=llm_config,
            execution_mode=execution_mode,
            query_plan=query_plan,
            doctor_id=doctor_id,
            explicit_doctor=explicit_doctor,
            time_slots=time_slots,
            resolved_ranges=resolved_ranges,
            planned_query_source=planned_query_source or PlannedQuerySource(source="fixed_template"),
        )

    def _run_doctors_with_active_plans(
        self,
        request: OrchestratorRequest,
        decision: IntentDecision,
        *,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        planned_query_source: PlannedQuerySource | None = None,
    ) -> OrchestratorResponse:
        question = (request.raw_text or "").strip()
        time_slots = self._extract_time_slots(question, request)
        resolved_ranges = self._resolve_time_slots(time_slots)
        recent_window = resolved_ranges.recent_window
        if recent_window is None:
            return self._build_not_supported_response(
                request,
                decision,
                llm_config=llm_config,
                execution_mode=execution_mode,
                reason="This subtype needs a resolvable recent window.",
                time_slots=time_slots,
                resolved_ranges=resolved_ranges,
            )

        recent_start_date = self._date_portion(recent_window.start)
        recent_end_date = self._date_portion(recent_window.end)
        query_plan = self._build_query_plan(
            normalized_question="List doctors with active patient training plans in the recent window.",
            subtype="doctors_with_active_plans",
            analysis_scope="doctor_aggregate",
            doctor_id=None,
            time_slots=time_slots,
            resolved_ranges=resolved_ranges,
            steps=[QueryPlanStep(step_id="step_1", intent="doctor_active_plan_aggregate", tool_name="list_doctors_with_active_plans", arguments={"start_date": recent_start_date, "end_date": recent_end_date}, rationale="Aggregate active plan counts by doctor without inheriting a session doctor filter.")],
        )

        step_results: list[StepExecutionResult] = []
        validation_issues: list[str] = []
        aggregate_payload = self._execute_step(step_results=step_results, step_id="step_1", tool_name="list_doctors_with_active_plans", args=query_plan.steps[0].arguments, mode=mode)
        result_rows = [DoctorAnalyticsResultRow.model_validate(item) for item in (aggregate_payload or []) if isinstance(item, dict)]
        summary = (
            f"Found {len(result_rows)} doctors with active patient training plans in {recent_start_date} to {recent_end_date}."
            if result_rows
            else f"No doctors with active patient training plans were found in {recent_start_date} to {recent_end_date}."
        )
        structured_output = AnalyticsStructuredOutput(
            question=question,
            subtype="doctors_with_active_plans",
            analysis_scope="doctor_aggregate",
            doctor_id=None,
            time_range=self._time_range_from_resolved_window(recent_window),
            time_slots=time_slots,
            resolved_ranges=resolved_ranges,
            source_backend=self.analytics_service.repository.last_backend,
            planned_query_source=planned_query_source or PlannedQuerySource(source="fixed_template"),
            query_plan=query_plan,
            historical_seen_set=None,
            recent_seen_set=None,
            absent_set=None,
            ranked_patients=None,
            result_rows=result_rows,
            evidence_basis=self._build_evidence_basis(subtype="doctors_with_active_plans"),
            summary=summary,
        )
        final_text = self._render_final_text(structured_output, explicit_doctor=False)
        validation_issues.extend(self._validate(structured_output, final_text))
        return OrchestratorResponse(success=True, task_type="open_analytics_query", execution_mode=execution_mode, llm_provider=llm_config.provider, llm_model=llm_config.model, structured_output=structured_output.model_dump(mode="json"), final_text=final_text, validation_issues=validation_issues, execution_trace=step_results)

    def _execute_query_plan(
        self,
        *,
        question: str,
        request: OrchestratorRequest,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        query_plan: QueryPlan,
        explicit_doctor: bool,
        planned_query_source: PlannedQuerySource,
        strict_failures: bool,
    ) -> OrchestratorResponse:
        if query_plan.analysis_scope == "doctor_aggregate":
            return self._execute_doctor_aggregate_query_plan(
                question=question,
                mode=mode,
                llm_config=llm_config,
                execution_mode=execution_mode,
                query_plan=query_plan,
                planned_query_source=planned_query_source,
                strict_failures=strict_failures,
            )
        if query_plan.subtype in PATIENT_ANALYSIS_SUBTYPES:
            if query_plan.doctor_id is None:
                raise ValueError("query_plan.missing_doctor_id")
            return self._execute_patient_query_plan(
                question=question,
                request=request,
                mode=mode,
                llm_config=llm_config,
                execution_mode=execution_mode,
                query_plan=query_plan,
                explicit_doctor=explicit_doctor,
                planned_query_source=planned_query_source,
                strict_failures=strict_failures,
            )
        raise ValueError(f"query_plan.unsupported_subtype:{query_plan.subtype}")

    def _execute_doctor_aggregate_query_plan(
        self,
        *,
        question: str,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        query_plan: QueryPlan,
        planned_query_source: PlannedQuerySource,
        strict_failures: bool,
    ) -> OrchestratorResponse:
        recent_window = query_plan.resolved_ranges.recent_window if query_plan.resolved_ranges else None
        if recent_window is None:
            raise ValueError("query_plan.missing_recent_window")
        step = next((item for item in query_plan.steps if item.tool_name == "list_doctors_with_active_plans"), None)
        if step is None:
            raise ValueError("query_plan.missing_doctor_aggregate_step")

        step_results: list[StepExecutionResult] = []
        aggregate_payload = self._execute_step_checked(
            step_results=step_results,
            step_id=step.step_id,
            tool_name=step.tool_name,
            args=dict(step.arguments),
            mode=mode,
            strict=strict_failures,
        )
        result_rows = [DoctorAnalyticsResultRow.model_validate(item) for item in (aggregate_payload or []) if isinstance(item, dict)]
        recent_start_date = self._date_portion(recent_window.start)
        recent_end_date = self._date_portion(recent_window.end)
        summary = (
            f"Found {len(result_rows)} doctors with active patient training plans in {recent_start_date} to {recent_end_date}."
            if result_rows
            else f"No doctors with active patient training plans were found in {recent_start_date} to {recent_end_date}."
        )
        structured_output = AnalyticsStructuredOutput(
            question=question,
            subtype=query_plan.subtype,
            analysis_scope="doctor_aggregate",
            doctor_id=None,
            time_range=self._time_range_from_resolved_window(recent_window),
            time_slots=query_plan.time_slots,
            resolved_ranges=query_plan.resolved_ranges,
            source_backend=self.analytics_service.repository.last_backend,
            planned_query_source=planned_query_source,
            query_plan=query_plan,
            historical_seen_set=None,
            recent_seen_set=None,
            absent_set=None,
            ranked_patients=None,
            result_rows=result_rows,
            evidence_basis=self._build_evidence_basis(subtype="doctors_with_active_plans"),
            summary=summary,
        )
        final_text = self._render_final_text(structured_output, explicit_doctor=False)
        validation_issues = self._validate(structured_output, final_text)
        return OrchestratorResponse(success=True, task_type="open_analytics_query", execution_mode=execution_mode, llm_provider=llm_config.provider, llm_model=llm_config.model, structured_output=structured_output.model_dump(mode="json"), final_text=final_text, validation_issues=validation_issues, execution_trace=step_results)

    def _execute_patient_query_plan(
        self,
        *,
        question: str,
        request: OrchestratorRequest,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        query_plan: QueryPlan,
        explicit_doctor: bool,
        planned_query_source: PlannedQuerySource,
        strict_failures: bool,
    ) -> OrchestratorResponse:
        recent_window = query_plan.resolved_ranges.recent_window if query_plan.resolved_ranges else None
        if recent_window is None:
            raise ValueError("query_plan.missing_recent_window")
        doctor_id = query_plan.doctor_id
        if doctor_id is None:
            raise ValueError("query_plan.missing_doctor_id")

        recent_start_date = self._date_portion(recent_window.start)
        recent_end_date = self._date_portion(recent_window.end)
        step_results: list[StepExecutionResult] = []
        outputs: dict[str, Any] = {}
        historical_seen: dict[str, Any] | None = None
        recent_seen: dict[str, Any] | None = None
        active_plans: dict[str, Any] | None = None
        absent_candidates: dict[str, Any] | None = None
        ranked_payload: dict[str, Any] | None = None
        last_visit_enriched = False
        plan_status_enriched = False

        for step in query_plan.steps:
            args = self._resolve_query_plan_step_args(
                step=step,
                outputs=outputs,
                historical_seen=historical_seen,
                recent_seen=recent_seen,
                absent_candidates=absent_candidates,
            )
            if step.tool_name in {"get_patient_last_visit", "get_patient_plan_status"} and "patient_id" not in args:
                patient_ids = self._patient_ids_from_step_args(args=args, outputs=outputs, fallback_set=absent_candidates)
                for patient_id in patient_ids:
                    fanout_args = dict(args)
                    fanout_args.pop("patient_set_ref", None)
                    fanout_args.pop("patient_ids_ref", None)
                    fanout_args["patient_id"] = patient_id
                    self._execute_step_checked(
                        step_results=step_results,
                        step_id=f"{step.step_id}_patient_{patient_id}",
                        tool_name=step.tool_name,
                        args=fanout_args,
                        mode=mode,
                        strict=strict_failures,
                    )
                outputs[step.step_id] = {"patient_ids": patient_ids}
                if step.tool_name == "get_patient_last_visit":
                    last_visit_enriched = True
                if step.tool_name == "get_patient_plan_status":
                    plan_status_enriched = True
                continue
            if step.tool_name == "rank_patients" and not args.get("patient_ids"):
                args["patient_ids"] = self._patient_ids_from_step_args(args=args, outputs=outputs, fallback_set=absent_candidates)
            if step.tool_name == "rank_patients" and absent_candidates is not None:
                patient_ids_for_rank = [int(item) for item in args.get("patient_ids") or absent_candidates.get("patient_ids") or []]
                if patient_ids_for_rank and not last_visit_enriched:
                    for patient_id in patient_ids_for_rank:
                        self._execute_step_checked(
                            step_results=step_results,
                            step_id=f"auto_last_visit_before_rank_{patient_id}",
                            tool_name="get_patient_last_visit",
                            args={"patient_id": patient_id, "doctor_id": doctor_id},
                            mode=mode,
                            strict=strict_failures,
                        )
                    last_visit_enriched = True
                if patient_ids_for_rank and not plan_status_enriched:
                    for patient_id in patient_ids_for_rank:
                        self._execute_step_checked(
                            step_results=step_results,
                            step_id=f"auto_plan_status_before_rank_{patient_id}",
                            tool_name="get_patient_plan_status",
                            args={"patient_id": patient_id, "doctor_id": doctor_id, "start_date": recent_start_date, "end_date": recent_end_date},
                            mode=mode,
                            strict=strict_failures,
                        )
                    plan_status_enriched = True

            payload = self._execute_step_checked(
                step_results=step_results,
                step_id=step.step_id,
                tool_name=step.tool_name,
                args=args,
                mode=mode,
                strict=strict_failures,
            )
            outputs[step.step_id] = payload
            if step.tool_name == "list_patients_seen_by_doctor" and isinstance(payload, dict):
                role = self._patient_set_role(step, historical_seen=historical_seen, recent_seen=recent_seen)
                if role == "recent":
                    recent_seen = payload
                else:
                    historical_seen = payload
            elif step.tool_name == "list_patients_with_active_plans" and isinstance(payload, dict):
                active_plans = payload
            elif step.tool_name == "set_diff" and isinstance(payload, dict):
                absent_candidates = payload
            elif step.tool_name == "rank_patients" and isinstance(payload, dict):
                ranked_payload = payload
            elif step.tool_name == "get_patient_last_visit":
                last_visit_enriched = True
            elif step.tool_name == "get_patient_plan_status":
                plan_status_enriched = True

        if absent_candidates is None and historical_seen is not None and recent_seen is not None:
            absent_candidates = self._execute_step_checked(
                step_results=step_results,
                step_id="auto_absent_diff",
                tool_name="set_diff",
                args={"base_set_id": historical_seen.get("set_id"), "subtract_set_id": recent_seen.get("set_id")},
                mode=mode,
                strict=strict_failures,
            )

        absent_patient_ids = (absent_candidates or {}).get("patient_ids") or []
        active_plan_set = PatientSet.model_validate(active_plans) if isinstance(active_plans, dict) else None
        if absent_patient_ids and ranked_payload is None:
            if not last_visit_enriched:
                for patient_id in absent_patient_ids:
                    self._execute_step_checked(
                        step_results=step_results,
                        step_id=f"auto_last_visit_patient_{patient_id}",
                        tool_name="get_patient_last_visit",
                        args={"patient_id": patient_id, "doctor_id": doctor_id},
                        mode=mode,
                        strict=strict_failures,
                    )
            if not plan_status_enriched:
                for patient_id in absent_patient_ids:
                    self._execute_step_checked(
                        step_results=step_results,
                        step_id=f"auto_plan_status_patient_{patient_id}",
                        tool_name="get_patient_plan_status",
                        args={"patient_id": patient_id, "doctor_id": doctor_id, "start_date": recent_start_date, "end_date": recent_end_date},
                        mode=mode,
                        strict=strict_failures,
                    )
            ranked_payload = self._execute_step_checked(
                step_results=step_results,
                step_id="auto_rank_patients",
                tool_name="rank_patients",
                args={"patient_ids": absent_patient_ids, "strategy": "active_plan_but_absent", "top_k": request.top_k or 20},
                mode=mode,
                strict=strict_failures,
            )

        if not absent_patient_ids:
            structured_output = AnalyticsStructuredOutput(
                question=question,
                subtype=query_plan.subtype,
                analysis_scope=query_plan.analysis_scope,
                doctor_id=doctor_id,
                time_range=self._time_range_from_resolved_window(recent_window),
                time_slots=query_plan.time_slots,
                resolved_ranges=query_plan.resolved_ranges,
                source_backend=self.analytics_service.repository.last_backend,
                planned_query_source=planned_query_source,
                query_plan=query_plan,
                historical_seen_set=PatientSet.model_validate(historical_seen) if isinstance(historical_seen, dict) else None,
                recent_seen_set=PatientSet.model_validate(recent_seen) if isinstance(recent_seen, dict) else None,
                absent_set=PatientSet.model_validate(absent_candidates) if isinstance(absent_candidates, dict) else None,
                ranked_patients=None,
                result_rows=[],
                evidence_basis=self._build_evidence_basis(subtype=query_plan.subtype, active_plan_set=active_plan_set),
                summary=f"No absent patients were found for doctor {doctor_id} in {recent_start_date} to {recent_end_date}.",
            )
            final_text = self._render_final_text(structured_output, explicit_doctor=explicit_doctor)
            validation_issues = self._validate(structured_output, final_text)
            return OrchestratorResponse(success=True, task_type="open_analytics_query", execution_mode=execution_mode, llm_provider=llm_config.provider, llm_model=llm_config.model, structured_output=structured_output.model_dump(mode="json"), final_text=final_text, validation_issues=validation_issues, execution_trace=step_results)

        ranked_patients = (
            RankedPatients.model_validate(ranked_payload)
            if isinstance(ranked_payload, dict)
            else self.analytics_service.rank_patients(patient_ids=absent_patient_ids, strategy="active_plan_but_absent", top_k=request.top_k or 20)
        )
        result_rows = self.analytics_service.build_result_rows(ranked_patients)
        active_plan_absent_count = sum(1 for row in result_rows if row.has_active_plan_in_window)
        summary = f"Found {len(absent_patient_ids)} absent patients for doctor {doctor_id} in {recent_start_date} to {recent_end_date}. {active_plan_absent_count} still have active plans in the recent window."
        if len(result_rows) < len(absent_patient_ids):
            summary += f" Showing top {len(result_rows)}."

        structured_output = AnalyticsStructuredOutput(
            question=question,
            subtype=query_plan.subtype,
            analysis_scope=query_plan.analysis_scope,
            doctor_id=doctor_id,
            time_range=self._time_range_from_resolved_window(recent_window),
            time_slots=query_plan.time_slots,
            resolved_ranges=query_plan.resolved_ranges,
            source_backend=self.analytics_service.repository.last_backend,
            planned_query_source=planned_query_source,
            query_plan=query_plan,
            historical_seen_set=PatientSet.model_validate(historical_seen) if isinstance(historical_seen, dict) else None,
            recent_seen_set=PatientSet.model_validate(recent_seen) if isinstance(recent_seen, dict) else None,
            absent_set=PatientSet.model_validate(absent_candidates) if isinstance(absent_candidates, dict) else None,
            ranked_patients=ranked_patients,
            result_rows=result_rows,
            evidence_basis=self._build_evidence_basis(subtype=query_plan.subtype, active_plan_set=active_plan_set),
            summary=summary,
        )
        final_text = self._render_final_text(structured_output, explicit_doctor=explicit_doctor)
        validation_issues = self._validate(structured_output, final_text)
        return OrchestratorResponse(success=True, task_type="open_analytics_query", execution_mode=execution_mode, llm_provider=llm_config.provider, llm_model=llm_config.model, structured_output=structured_output.model_dump(mode="json"), final_text=final_text, validation_issues=validation_issues, execution_trace=step_results)

    def _execute_absent_patient_analysis(
        self,
        *,
        question: str,
        request: OrchestratorRequest,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        query_plan: QueryPlan,
        doctor_id: int,
        explicit_doctor: bool,
        time_slots: AnalyticsTimeSlots,
        resolved_ranges: ResolvedAnalyticsRanges,
        planned_query_source: PlannedQuerySource | None = None,
    ) -> OrchestratorResponse:
        recent_window = resolved_ranges.recent_window
        assert recent_window is not None
        recent_start_date = self._date_portion(recent_window.start)
        recent_end_date = self._date_portion(recent_window.end)

        step_results: list[StepExecutionResult] = []
        validation_issues: list[str] = []
        base_seen = self._execute_step(step_results=step_results, step_id="step_1", tool_name="list_patients_seen_by_doctor", args=query_plan.steps[0].arguments, mode=mode)
        recent_seen = self._execute_step(step_results=step_results, step_id="step_2", tool_name="list_patients_seen_by_doctor", args=query_plan.steps[1].arguments, mode=mode)
        active_plans = self._execute_step(step_results=step_results, step_id="step_3", tool_name="list_patients_with_active_plans", args=query_plan.steps[2].arguments, mode=mode)
        absent_candidates = self._execute_step(
            step_results=step_results,
            step_id="step_4",
            tool_name="set_diff",
            args={"base_set_id": base_seen.get("set_id") if isinstance(base_seen, dict) else "", "subtract_set_id": recent_seen.get("set_id") if isinstance(recent_seen, dict) else ""},
            mode=mode,
        )

        absent_patient_ids = (absent_candidates or {}).get("patient_ids") or []
        active_plan_set = PatientSet.model_validate(active_plans) if isinstance(active_plans, dict) else None
        if not absent_patient_ids:
            structured_output = AnalyticsStructuredOutput(
                question=question,
                subtype=query_plan.subtype,
                analysis_scope=query_plan.analysis_scope,
                doctor_id=doctor_id,
                time_range=self._time_range_from_resolved_window(recent_window),
                time_slots=time_slots,
                resolved_ranges=resolved_ranges,
                source_backend=self.analytics_service.repository.last_backend,
                planned_query_source=planned_query_source or PlannedQuerySource(source="fixed_template"),
                query_plan=query_plan,
                historical_seen_set=PatientSet.model_validate(base_seen) if isinstance(base_seen, dict) else None,
                recent_seen_set=PatientSet.model_validate(recent_seen) if isinstance(recent_seen, dict) else None,
                absent_set=PatientSet.model_validate(absent_candidates) if isinstance(absent_candidates, dict) else None,
                ranked_patients=None,
                result_rows=[],
                evidence_basis=self._build_evidence_basis(subtype=query_plan.subtype, active_plan_set=active_plan_set),
                summary=f"No absent patients were found for doctor {doctor_id} in {recent_start_date} to {recent_end_date}.",
            )
            final_text = self._render_final_text(structured_output, explicit_doctor=explicit_doctor)
            validation_issues.extend(self._validate(structured_output, final_text))
            return OrchestratorResponse(success=True, task_type="open_analytics_query", execution_mode=execution_mode, llm_provider=llm_config.provider, llm_model=llm_config.model, structured_output=structured_output.model_dump(mode="json"), final_text=final_text, validation_issues=validation_issues, execution_trace=step_results)

        for patient_id in absent_patient_ids:
            self._execute_step(step_results=step_results, step_id=f"step_5_patient_{patient_id}", tool_name="get_patient_last_visit", args={"patient_id": patient_id, "doctor_id": doctor_id}, mode=mode)
        for patient_id in absent_patient_ids:
            self._execute_step(
                step_results=step_results,
                step_id=f"step_6_patient_{patient_id}",
                tool_name="get_patient_plan_status",
                args={"patient_id": patient_id, "doctor_id": doctor_id, "start_date": recent_start_date, "end_date": recent_end_date},
                mode=mode,
            )
        ranked_payload = self._execute_step(
            step_results=step_results,
            step_id="step_7",
            tool_name="rank_patients",
            args={"patient_ids": absent_patient_ids, "strategy": "active_plan_but_absent", "top_k": request.top_k or 20},
            mode=mode,
        )
        ranked_patients = (
            RankedPatients.model_validate(ranked_payload)
            if isinstance(ranked_payload, dict)
            else self.analytics_service.rank_patients(patient_ids=absent_patient_ids, strategy="active_plan_but_absent", top_k=request.top_k or 20)
        )
        result_rows = self.analytics_service.build_result_rows(ranked_patients)
        active_plan_absent_count = sum(1 for row in result_rows if row.has_active_plan_in_window)
        total_absent_count = len(absent_patient_ids)
        summary = f"Found {total_absent_count} absent patients for doctor {doctor_id} in {recent_start_date} to {recent_end_date}. {active_plan_absent_count} still have active plans in the recent window."
        if len(result_rows) < total_absent_count:
            summary += f" Showing top {len(result_rows)}."

        structured_output = AnalyticsStructuredOutput(
            question=question,
            subtype=query_plan.subtype,
            analysis_scope=query_plan.analysis_scope,
            doctor_id=doctor_id,
            time_range=self._time_range_from_resolved_window(recent_window),
            time_slots=time_slots,
            resolved_ranges=resolved_ranges,
            source_backend=self.analytics_service.repository.last_backend,
            planned_query_source=planned_query_source or PlannedQuerySource(source="fixed_template"),
            query_plan=query_plan,
            historical_seen_set=PatientSet.model_validate(base_seen) if isinstance(base_seen, dict) else None,
            recent_seen_set=PatientSet.model_validate(recent_seen) if isinstance(recent_seen, dict) else None,
            absent_set=PatientSet.model_validate(absent_candidates) if isinstance(absent_candidates, dict) else None,
            ranked_patients=ranked_patients,
            result_rows=result_rows,
            evidence_basis=self._build_evidence_basis(subtype=query_plan.subtype, active_plan_set=active_plan_set),
            summary=summary,
        )
        final_text = self._render_final_text(structured_output, explicit_doctor=explicit_doctor)
        validation_issues.extend(self._validate(structured_output, final_text))
        return OrchestratorResponse(success=True, task_type="open_analytics_query", execution_mode=execution_mode, llm_provider=llm_config.provider, llm_model=llm_config.model, structured_output=structured_output.model_dump(mode="json"), final_text=final_text, validation_issues=validation_issues, execution_trace=step_results)

    def _build_not_supported_response(
        self,
        request: OrchestratorRequest,
        decision: IntentDecision,
        *,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
        reason: str,
        time_slots: AnalyticsTimeSlots | None = None,
        resolved_ranges: ResolvedAnalyticsRanges | None = None,
        planned_query_source: PlannedQuerySource | None = None,
    ) -> OrchestratorResponse:
        question = (request.raw_text or "").strip()
        query_plan = self._build_query_plan(
            normalized_question="Unsupported or ambiguous open analytics request.",
            subtype=decision.analytics_subtype,
            analysis_scope=decision.analysis_scope,
            doctor_id=None,
            time_slots=time_slots,
            resolved_ranges=resolved_ranges,
            steps=[],
        )
        structured_output = AnalyticsStructuredOutput(
            question=question,
            subtype=decision.analytics_subtype,
            analysis_scope=decision.analysis_scope,
            doctor_id=None,
            time_range=None,
            time_slots=time_slots,
            resolved_ranges=resolved_ranges,
            source_backend=self.analytics_service.repository.last_backend,
            planned_query_source=planned_query_source or PlannedQuerySource(source="fixed_template"),
            query_plan=query_plan,
            historical_seen_set=None,
            recent_seen_set=None,
            absent_set=None,
            ranked_patients=None,
            result_rows=[],
            evidence_basis=[],
            supported_subtypes=list(SUPPORTED_ANALYTICS_SUBTYPES),
            summary=reason,
        )
        final_text = self._render_final_text(structured_output, explicit_doctor=False)
        return OrchestratorResponse(success=False, task_type="open_analytics_query", execution_mode=execution_mode, llm_provider=llm_config.provider, llm_model=llm_config.model, structured_output=structured_output.model_dump(mode="json"), final_text=final_text, validation_issues=["analytics.subtype.unsupported_or_ambiguous"], execution_trace=[])

    def _build_query_plan(
        self,
        *,
        normalized_question: str,
        subtype: OpenAnalyticsSubtype | None,
        analysis_scope: AnalyticsScope | None,
        doctor_id: int | None,
        time_slots: AnalyticsTimeSlots | None,
        resolved_ranges: ResolvedAnalyticsRanges | None,
        steps: list[QueryPlanStep],
    ) -> QueryPlan:
        recent_window = resolved_ranges.recent_window if resolved_ranges else None
        baseline_window = resolved_ranges.baseline_window if resolved_ranges else None
        return QueryPlan(
            normalized_question=normalized_question,
            subtype=subtype,
            analysis_scope=analysis_scope,
            doctor_id=doctor_id,
            start_date=self._date_portion(recent_window.start) if recent_window else None,
            end_date=self._date_portion(recent_window.end) if recent_window else None,
            recent_start_date=self._date_portion(recent_window.start) if recent_window else None,
            recent_end_date=self._date_portion(recent_window.end) if recent_window else None,
            baseline_start_date=self._date_portion(baseline_window.start) if baseline_window else None,
            baseline_end_date=self._date_portion(baseline_window.end) if baseline_window else None,
            time_parse_mode=time_slots.parse_mode if time_slots else None,
            time_slots=time_slots,
            resolved_ranges=resolved_ranges,
            steps=steps,
        )

    def _resolve_doctor_context(self, request: OrchestratorRequest, *, analysis_scope: AnalyticsScope) -> tuple[int | None, bool]:
        explicit_question_doctor = self._extract_doctor_id((request.raw_text or "").strip())
        context_doctor = self._coerce_int((request.context or {}).get("therapist_id"))
        request_doctor = request.therapist_id
        explicit_doctor = explicit_question_doctor is not None or (request_doctor is not None and context_doctor != request_doctor)
        if analysis_scope == "doctor_aggregate":
            return None, explicit_doctor
        doctor_id = explicit_question_doctor or request_doctor or context_doctor or self.settings.demo_default_therapist_id
        return doctor_id, explicit_doctor

    def _extract_time_slots(self, question: str, request: OrchestratorRequest) -> AnalyticsTimeSlots:
        if request.analytics_time_slots is not None:
            return request.analytics_time_slots

        dual_window_slots = self._try_parse_dual_window_slots(question, request=request)
        if dual_window_slots is not None:
            return dual_window_slots

        explicit_recent_days = self._extract_explicit_recent_days(question)
        if explicit_recent_days is not None:
            return AnalyticsTimeSlots(recent_window=RelativeWindow(start_offset_days=-explicit_recent_days, end_offset_days=0, label=f"recent {explicit_recent_days} days"), raw_days=explicit_recent_days, parse_mode="single_window")

        legacy_days = request.days or self._extract_days(question)
        if legacy_days is not None:
            return AnalyticsTimeSlots(recent_window=RelativeWindow(start_offset_days=-legacy_days, end_offset_days=0, label=f"recent {legacy_days} days"), raw_days=legacy_days, parse_mode="fallback", note="Fell back to legacy days extraction.")

        default_days = self.settings.default_time_window_days
        return AnalyticsTimeSlots(recent_window=RelativeWindow(start_offset_days=-default_days, end_offset_days=0, label=f"recent {default_days} days"), raw_days=default_days, parse_mode="fallback", note="Used the default analytics time window.")

    def _try_parse_dual_window_slots(self, question: str, *, request: OrchestratorRequest) -> AnalyticsTimeSlots | None:
        recent_days = self._extract_explicit_recent_days(question) or request.days
        range_match = re.search(r"前\s*(\d+)\s*[-到至]\s*(\d+)\s*天", question, flags=re.IGNORECASE)
        if not range_match:
            range_match = re.search(r"前\s*(\d+)\s*天\s*(?:到|至|-)\s*前\s*(\d+)\s*天", question, flags=re.IGNORECASE)
        if range_match:
            older_days = int(range_match.group(1))
            newer_days = int(range_match.group(2))
            if older_days < newer_days:
                older_days, newer_days = newer_days, older_days
            effective_recent_days = recent_days or newer_days
            return AnalyticsTimeSlots(
                recent_window=RelativeWindow(start_offset_days=-effective_recent_days, end_offset_days=0, label=f"recent {effective_recent_days} days"),
                baseline_window=RelativeWindow(start_offset_days=-older_days, end_offset_days=-newer_days, label=f"baseline {older_days}-{newer_days} days ago"),
                raw_days=effective_recent_days,
                parse_mode="dual_window",
            )

        exclude_match = re.search(r"过去\s*(\d+)\s*天.*(?:排除|除去|除掉|去掉).*(?:最近|这)?\s*(\d+)\s*天", question, flags=re.IGNORECASE)
        if exclude_match:
            total_days = int(exclude_match.group(1))
            excluded_recent_days = int(exclude_match.group(2))
            if total_days > excluded_recent_days:
                return AnalyticsTimeSlots(
                    recent_window=RelativeWindow(start_offset_days=-excluded_recent_days, end_offset_days=0, label=f"recent {excluded_recent_days} days"),
                    baseline_window=RelativeWindow(start_offset_days=-total_days, end_offset_days=-excluded_recent_days, label=f"baseline {total_days}-{excluded_recent_days} days ago"),
                    raw_days=excluded_recent_days,
                    parse_mode="dual_window",
                )

        lowered = question.lower()
        if any(keyword in question or keyword in lowered for keyword in ("baseline", "基线", "前一阶段", "前一段时间")):
            if recent_days is None:
                return AnalyticsTimeSlots(raw_days=request.days, parse_mode="fallback", note="Baseline wording detected but exact offsets were not resolved.")
            return AnalyticsTimeSlots(recent_window=RelativeWindow(start_offset_days=-recent_days, end_offset_days=0, label=f"recent {recent_days} days"), raw_days=recent_days, parse_mode="fallback", note="Baseline wording detected but exact baseline offsets were not resolved.")
        return None

    def _resolve_time_slots(
        self,
        time_slots: AnalyticsTimeSlots,
        *,
        doctor_id: int | None = None,
        patient_id: int | None = None,
    ) -> ResolvedAnalyticsRanges:
        recent_window = time_slots.recent_window
        baseline_window = time_slots.baseline_window
        if recent_window and baseline_window is None and recent_window.end_offset_days in {0, None} and time_slots.raw_days:
            recent_time_range = build_time_range(self.analytics_service.repository, therapist_id=doctor_id, patient_id=patient_id, days=time_slots.raw_days)
            return ResolvedAnalyticsRanges(anchor_time=recent_time_range.end.isoformat(sep=" "), recent_window=self._resolved_window_from_datetimes(recent_time_range.start, recent_time_range.end, recent_time_range.label), baseline_window=None)

        anchor_time = resolve_time_anchor(self.analytics_service.repository, therapist_id=doctor_id, patient_id=patient_id)
        resolved_recent = self._resolve_relative_window(recent_window, anchor_time)
        resolved_baseline = self._resolve_relative_window(baseline_window, anchor_time)
        if resolved_recent and resolved_baseline and baseline_window and recent_window and baseline_window.end_offset_days == recent_window.start_offset_days:
            recent_start_dt = self._parse_required_datetime(resolved_recent.start)
            baseline_start_dt = self._parse_required_datetime(resolved_baseline.start)
            adjusted_baseline_end = recent_start_dt - timedelta(seconds=1)
            resolved_baseline = self._resolved_window_from_datetimes(baseline_start_dt, adjusted_baseline_end, f"{baseline_start_dt.date().isoformat()} to {adjusted_baseline_end.date().isoformat()}")
        return ResolvedAnalyticsRanges(anchor_time=anchor_time.isoformat(sep=" "), recent_window=resolved_recent, baseline_window=resolved_baseline)

    def _resolve_relative_window(self, window: RelativeWindow | None, anchor_time: datetime) -> ResolvedWindow | None:
        if window is None or window.start_offset_days is None:
            return None
        start_dt = datetime.combine((anchor_time + timedelta(days=window.start_offset_days)).date(), time.min)
        end_offset_days = 0 if window.end_offset_days is None else window.end_offset_days
        if end_offset_days == 0:
            end_dt = anchor_time
        else:
            end_dt = datetime.combine((anchor_time + timedelta(days=end_offset_days)).date(), time.max).replace(microsecond=0)
        return self._resolved_window_from_datetimes(start_dt, end_dt, window.label or f"{start_dt.date().isoformat()} to {end_dt.date().isoformat()}")

    def _resolved_window_from_datetimes(self, start_dt: datetime, end_dt: datetime, label: str) -> ResolvedWindow:
        return ResolvedWindow(start=start_dt.isoformat(sep=" "), end=end_dt.isoformat(sep=" "), label=label)

    def _execute_step_checked(
        self,
        *,
        step_results: list[StepExecutionResult],
        step_id: str,
        tool_name: str,
        args: dict[str, Any],
        mode: str,
        strict: bool,
    ) -> Any:
        before_count = len(step_results)
        payload = self._execute_step(
            step_results=step_results,
            step_id=step_id,
            tool_name=tool_name,
            args=args,
            mode=mode,
        )
        if strict and len(step_results) > before_count and not step_results[-1].success:
            raise RuntimeError(f"query_plan.step_failed:{step_id}:{tool_name}:{step_results[-1].error}")
        return payload

    def _resolve_query_plan_step_args(
        self,
        *,
        step: QueryPlanStep,
        outputs: dict[str, Any],
        historical_seen: dict[str, Any] | None,
        recent_seen: dict[str, Any] | None,
        absent_candidates: dict[str, Any] | None,
    ) -> dict[str, Any]:
        args = dict(step.arguments or {})
        if step.tool_name == "set_diff":
            if "base_set_ref" in args and "base_set_id" not in args:
                args["base_set_id"] = self._set_id_from_step_ref(args.pop("base_set_ref"), outputs)
            if "subtract_set_ref" in args and "subtract_set_id" not in args:
                args["subtract_set_id"] = self._set_id_from_step_ref(args.pop("subtract_set_ref"), outputs)
            if "base_set_id" not in args and historical_seen is not None:
                args["base_set_id"] = historical_seen.get("set_id")
            if "subtract_set_id" not in args and recent_seen is not None:
                args["subtract_set_id"] = recent_seen.get("set_id")
        if step.tool_name in {"get_patient_last_visit", "get_patient_plan_status", "rank_patients"}:
            if "patient_set_ref" in args:
                patient_ids = self._patient_ids_from_ref(args["patient_set_ref"], outputs)
                args["patient_ids_ref"] = args.pop("patient_set_ref")
                if step.tool_name == "rank_patients":
                    args.setdefault("patient_ids", patient_ids)
            if step.tool_name == "rank_patients" and "patient_ids_ref" in args:
                args.setdefault("patient_ids", self._patient_ids_from_ref(args["patient_ids_ref"], outputs))
            if step.tool_name == "rank_patients" and "patient_ids" not in args and absent_candidates is not None:
                args["patient_ids"] = absent_candidates.get("patient_ids") or []
            if step.tool_name == "rank_patients":
                args.pop("patient_set_ref", None)
                args.pop("patient_ids_ref", None)
        return args

    def _set_id_from_step_ref(self, step_ref: Any, outputs: dict[str, Any]) -> str:
        if not isinstance(step_ref, str):
            raise ValueError(f"invalid_step_ref:{step_ref}")
        output = outputs.get(step_ref)
        if not isinstance(output, dict) or not output.get("set_id"):
            raise ValueError(f"step_ref_has_no_set_id:{step_ref}")
        return str(output["set_id"])

    def _patient_ids_from_ref(self, step_ref: Any, outputs: dict[str, Any]) -> list[int]:
        if not isinstance(step_ref, str):
            raise ValueError(f"invalid_step_ref:{step_ref}")
        output = outputs.get(step_ref)
        if isinstance(output, dict):
            if isinstance(output.get("patient_ids"), list):
                return [int(item) for item in output["patient_ids"]]
            if isinstance(output.get("rows"), list):
                return [int(item["patient_id"]) for item in output["rows"] if isinstance(item, dict) and item.get("patient_id") is not None]
        raise ValueError(f"step_ref_has_no_patient_ids:{step_ref}")

    def _patient_ids_from_step_args(
        self,
        *,
        args: dict[str, Any],
        outputs: dict[str, Any],
        fallback_set: dict[str, Any] | None,
    ) -> list[int]:
        if isinstance(args.get("patient_ids"), list):
            return [int(item) for item in args["patient_ids"]]
        ref = args.get("patient_set_ref") or args.get("patient_ids_ref")
        if ref:
            return self._patient_ids_from_ref(ref, outputs)
        if fallback_set is not None:
            return [int(item) for item in fallback_set.get("patient_ids") or []]
        return []

    def _patient_set_role(
        self,
        step: QueryPlanStep,
        *,
        historical_seen: dict[str, Any] | None,
        recent_seen: dict[str, Any] | None,
    ) -> str:
        text = f"{step.step_id} {step.intent} {step.rationale}".lower()
        if "recent" in text:
            return "recent"
        if any(token in text for token in ("baseline", "historical", "history", "prior", "old")):
            return "historical"
        if historical_seen is None:
            return "historical"
        if recent_seen is None:
            return "recent"
        return "historical"

    def _execute_step(self, *, step_results: list[StepExecutionResult], step_id: str, tool_name: str, args: dict[str, Any], mode: str) -> Any:
        tool = self.analytics_tool_registry.get(tool_name)
        if tool is None:
            raise ValueError(f"tool_not_allowed:{tool_name}")
        try:
            raw_output = tool.invoke(mode=mode, args=args)
            step_results.append(StepExecutionResult(step_id=step_id, tool_name=tool_name, success=True, args=args, output_summary=self._summarize_output(tool_name, raw_output), raw_output=raw_output))
            return raw_output
        except Exception as exc:  # noqa: BLE001
            step_results.append(StepExecutionResult(step_id=step_id, tool_name=tool_name, success=False, args=args, output_summary="step failed", raw_output=None, error=str(exc)))
            return {} if tool_name != "list_doctors_with_active_plans" else []

    def _summarize_output(self, tool_name: str, payload: Any) -> str:
        if tool_name in {"list_patients_seen_by_doctor", "list_patients_with_active_plans", "set_diff"} and isinstance(payload, dict):
            return f"patient set count={payload.get('count', 0)}"
        if tool_name == "list_doctors_with_active_plans":
            return f"doctor rows={len(payload or [])}" if isinstance(payload, list) else "doctor rows=0"
        if tool_name == "get_patient_last_visit" and isinstance(payload, dict):
            return f"last_visit={payload.get('last_visit_time') or 'NA'}"
        if tool_name == "get_patient_plan_status" and isinstance(payload, dict):
            return f"active_plan={payload.get('has_active_plan')} planned={payload.get('planned_sessions')} attended={payload.get('attended_sessions')}"
        if tool_name == "rank_patients" and isinstance(payload, dict):
            return f"ranked_rows={len(payload.get('rows') or [])}"
        return "tool finished"

    def _render_final_text(self, output: AnalyticsStructuredOutput, *, explicit_doctor: bool) -> str:
        lines = [
            "Open analytics result",
            f"Question: {output.question or 'N/A'}",
            f"Subtype: {output.subtype or 'unclassified'}",
            f"Scope: {output.analysis_scope or 'unknown'}",
        ]
        if output.analysis_scope == "doctor_aggregate":
            lines.append("Doctor scope: all doctors")
        else:
            lines.append(f"Doctor: {output.doctor_id if output.doctor_id is not None else 'N/A'}")
            if not explicit_doctor and output.doctor_id is not None:
                lines.append(f"Note: no explicit doctor was provided, so doctor {output.doctor_id} was inherited from session/default context.")
        if output.resolved_ranges and output.resolved_ranges.recent_window:
            recent = output.resolved_ranges.recent_window
            lines.append(f"Recent window: {recent.label} ({self._date_portion(recent.start)} to {self._date_portion(recent.end)})")
        if output.resolved_ranges and output.resolved_ranges.baseline_window:
            baseline = output.resolved_ranges.baseline_window
            lines.append(f"Baseline window: {baseline.label} ({self._date_portion(baseline.start)} to {self._date_portion(baseline.end)})")
        lines.append(f"Summary: {output.summary}")

        if output.supported_subtypes:
            lines.append("Supported subtypes:")
            for subtype in output.supported_subtypes:
                lines.append(f"- {subtype}")

        if output.result_rows:
            if output.analysis_scope == "doctor_aggregate":
                lines.append("Doctor rows:")
                for index, row in enumerate(output.result_rows, start=1):
                    doctor_row = DoctorAnalyticsResultRow.model_validate(row)
                    lines.append(f"{index}. doctor {doctor_row.doctor_id} | active_plan_patient_count {doctor_row.active_plan_patient_count} | active_plan_count {doctor_row.active_plan_count} | {doctor_row.note or 'no note'}")
            else:
                lines.append("Patient rows:")
                for index, row in enumerate(output.result_rows, start=1):
                    lines.append(f"{index}. patient {row.patient_id} | last_visit {row.last_visit_time or 'NA'} | active_plan_in_window {self._localize_bool(row.has_active_plan_in_window)} | planned/attended {row.planned_sessions or 0}/{row.attended_sessions or 0} | {row.rank_reason or 'no ranking note'}")
        else:
            lines.append("Result rows: empty")

        if output.evidence_basis:
            lines.append("Evidence basis:")
            for item in output.evidence_basis:
                lines.append(f"- {item}")
        return "\n".join(lines)

    def _validate(self, output: AnalyticsStructuredOutput, final_text: str) -> list[str]:
        issues: list[str] = []
        if output.subtype in PATIENT_ANALYSIS_SUBTYPES and output.absent_set is None:
            issues.append("analytics.absent_set.missing")
        if output.analysis_scope == "doctor_aggregate" and output.doctor_id is not None:
            issues.append("analytics.aggregate_scope_should_not_have_doctor_id")
        if output.source_backend == "mock" and "real database" in final_text.lower():
            issues.append("analytics.mock_backend_misreported")
        if output.ranked_patients and len(output.result_rows) != len(output.ranked_patients.rows):
            issues.append("analytics.result_rows_count_mismatch")
        return issues

    def _build_evidence_basis(self, *, subtype: OpenAnalyticsSubtype | None, active_plan_set: PatientSet | None = None) -> list[str]:
        if subtype == "doctors_with_active_plans":
            return [
                "Doctor aggregates are grouped from dbrehaplan BookingTime/CreateTime records by DoctorId and UserId.",
                "No session doctor filter is applied for doctor_aggregate analyses.",
            ]
        items = [
            "Attendance cohorts come from read-only execution logs joined with plan doctor and patient IDs.",
            "Plan status comes from dbrehaplan records plus recent execution coverage in the same window.",
        ]
        if active_plan_set is not None:
            items.append(f"Recent active-plan patient set count: {active_plan_set.count}.")
        return items

    def _extract_doctor_id(self, text: str) -> int | None:
        match = re.search(r"(?:医生|治疗师|康复师|doctor|therapist)\s*(?:id)?\s*[:：]?\s*(\d+)", text, flags=re.IGNORECASE)
        return int(match.group(1)) if match else None

    def _extract_days(self, text: str) -> int | None:
        lowered = text.lower()
        if "本周" in text or "最近一周" in text or "近一周" in text or "last week" in lowered:
            return 7
        if "本月" in text or "最近一个月" in text or "近一个月" in text or "last month" in lowered:
            return 30
        for pattern in (r"(?:最近|过去|近)\s*(\d+)\s*天", r"last\s*(\d+)\s*days?", r"(\d+)\s*天"):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    def _extract_explicit_recent_days(self, text: str) -> int | None:
        lowered = text.lower()
        if "本周" in text or "最近一周" in text or "近一周" in text or "last week" in lowered:
            return 7
        if "本月" in text or "最近一个月" in text or "近一个月" in text or "last month" in lowered:
            return 30
        for pattern in (r"(?:最近|这|近|过去)\s*(\d+)\s*(?:天)?", r"last\s*(\d+)\s*days?"):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    def _time_range_from_resolved_window(self, window: ResolvedWindow) -> TimeRange:
        return TimeRange(start=self._parse_required_datetime(window.start), end=self._parse_required_datetime(window.end), label=window.label)

    def _parse_required_datetime(self, value: str) -> datetime:
        dt = parse_datetime_flexible(value)
        if dt is None:
            raise ValueError(f"invalid_datetime:{value}")
        return dt

    def _date_portion(self, value: str) -> str:
        return self._parse_required_datetime(value).date().isoformat()

    def _date_portion_from_datetime(self, value: datetime) -> str:
        return value.date().isoformat()

    def _coerce_int(self, value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _localize_bool(self, value: bool | None) -> str:
        if value is True:
            return "yes"
        if value is False:
            return "no"
        return "unknown"
