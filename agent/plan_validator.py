from __future__ import annotations

import json
import re
from typing import Any

from tools import ToolSpec

from .schemas import LLMPlannedQuery, LLMPlannedStep, PlanValidationIssue, PlanValidationResult, RoutedDecision


PATIENT_SET_TOOLS = {"list_patients_seen_by_doctor", "list_patients_with_active_plans", "set_diff"}
PATIENT_FANOUT_TOOLS = {"get_patient_last_visit", "get_patient_plan_status", "rank_patients"}
DOCTOR_AGGREGATE_TOOLS = {"list_doctors_with_active_plans"}
SINGLE_DOCTOR_REQUIRED_TOOLS = {"list_patients_seen_by_doctor", "list_patients_with_active_plans"}
SUPPORTED_RANK_STRATEGIES = {"active_plan_but_absent", "last_visit_oldest", "highest_risk"}
SQL_PATTERN = re.compile(r"\b(select|insert|update|delete|drop|alter|join|where|from)\b", re.IGNORECASE)


class PlanValidator:
    def __init__(self, tool_registry: dict[str, ToolSpec], *, max_steps: int = 8):
        self.tool_registry = tool_registry
        self.max_steps = max_steps

    def validate(
        self,
        plan: LLMPlannedQuery,
        *,
        routed_decision: RoutedDecision,
    ) -> PlanValidationResult:
        issues: list[PlanValidationIssue] = []
        normalized_steps: list[LLMPlannedStep] = []

        if plan.source != "llm_planner":
            issues.append(self._issue("plan.source.invalid", "Plan source must be llm_planner."))
        if not plan.steps:
            issues.append(self._issue("plan.steps.empty", "Planner returned no executable steps."))
        if len(plan.steps) > self.max_steps:
            issues.append(self._issue("plan.steps.too_many", f"Planner returned {len(plan.steps)} steps; max is {self.max_steps}."))

        seen_step_ids: set[str] = set()
        seen_signatures: set[str] = set()
        produced_patient_sets: set[str] = set()
        scope = routed_decision.final_scope

        for step in plan.steps:
            tool = self.tool_registry.get(step.tool_name)
            if not step.step_id:
                issues.append(self._issue("step.id.empty", "Step ID is required.", step.step_id or None))
            if step.step_id in seen_step_ids:
                issues.append(self._issue("step.id.duplicate", f"Duplicate step_id: {step.step_id}.", step.step_id))
            if tool is None:
                issues.append(self._issue("tool.unknown", f"Tool is not in planner whitelist: {step.tool_name}.", step.step_id))
            else:
                issues.extend(self._validate_scope(step, tool, routed_decision))
                issues.extend(self._validate_arguments(step, tool))

            issues.extend(self._validate_references(step, seen_step_ids))
            if self._contains_sql(step.arguments):
                issues.append(self._issue("arguments.sql_like", "Arguments contain SQL-like text, which is not allowed.", step.step_id))

            signature = self._signature(step)
            if signature in seen_signatures:
                issues.append(self._issue("step.duplicate_noop", "Duplicate tool call with the same arguments.", step.step_id))
            seen_signatures.add(signature)

            if step.tool_name in PATIENT_SET_TOOLS:
                produced_patient_sets.add(step.step_id)
            if step.tool_name in PATIENT_FANOUT_TOOLS:
                missing_reference = (
                    "patient_id" not in step.arguments
                    and "patient_ids" not in step.arguments
                    and "patient_set_ref" not in step.arguments
                    and "patient_ids_ref" not in step.arguments
                )
                if missing_reference:
                    issues.append(
                        self._issue(
                            "arguments.patient_reference_missing",
                            "Patient fan-out tools need patient_id, patient_ids, patient_set_ref, or patient_ids_ref.",
                            step.step_id,
                        )
                    )
            if step.tool_name == "rank_patients":
                strategy = step.arguments.get("strategy")
                if strategy not in SUPPORTED_RANK_STRATEGIES:
                    issues.append(
                        self._issue(
                            "arguments.rank_strategy.unsupported",
                            f"rank_patients.strategy must be one of {sorted(SUPPORTED_RANK_STRATEGIES)}.",
                            step.step_id,
                        )
                    )

            seen_step_ids.add(step.step_id)
            normalized_steps.append(step)

        if scope == "doctor_aggregate":
            patient_scope_tools = [step.tool_name for step in plan.steps if step.tool_name not in DOCTOR_AGGREGATE_TOOLS]
            if patient_scope_tools:
                issues.append(
                    self._issue(
                        "scope.doctor_aggregate.patient_tool",
                        f"doctor_aggregate plans may not use patient/single-doctor tools: {patient_scope_tools}.",
                    )
                )
            if not any(step.tool_name == "list_doctors_with_active_plans" for step in plan.steps):
                issues.append(
                    self._issue(
                        "scope.doctor_aggregate.missing_aggregate_tool",
                        "doctor_aggregate plans need list_doctors_with_active_plans.",
                    )
                )
        if scope == "single_doctor":
            doctor_tools_missing_scope = [
                step.step_id
                for step in plan.steps
                if step.tool_name in SINGLE_DOCTOR_REQUIRED_TOOLS and step.arguments.get("doctor_id") is None
            ]
            if doctor_tools_missing_scope:
                issues.append(
                    self._issue(
                        "scope.single_doctor.missing_doctor_id",
                        f"single_doctor plan is missing doctor_id in steps: {doctor_tools_missing_scope}.",
                    )
                )
            if plan.subtype in {"absent_old_patients_recent_window", "absent_from_baseline_window"} and not any(
                step.tool_name == "set_diff" for step in plan.steps
            ):
                issues.append(
                    self._issue(
                        "subtype.absent.missing_set_diff",
                        "Absent-patient plans need a set_diff step.",
                    )
                )

        return PlanValidationResult(
            is_valid=not issues,
            issues=issues,
            normalized_steps=normalized_steps if not issues else [],
        )

    def _validate_scope(
        self,
        step: LLMPlannedStep,
        tool: ToolSpec,
        routed_decision: RoutedDecision,
    ) -> list[PlanValidationIssue]:
        issues: list[PlanValidationIssue] = []
        scope = routed_decision.final_scope
        args = step.arguments or {}
        if scope == "doctor_aggregate":
            if step.tool_name not in DOCTOR_AGGREGATE_TOOLS:
                issues.append(
                    self._issue(
                        "tool.scope.invalid",
                        f"{step.tool_name} is not allowed in doctor_aggregate scope.",
                        step.step_id,
                    )
                )
            if args.get("doctor_id") is not None or args.get("therapist_id") is not None:
                issues.append(
                    self._issue(
                        "scope.doctor_aggregate.doctor_filter",
                        "doctor_aggregate plans must not inherit or inject a single doctor filter.",
                        step.step_id,
                    )
                )
        elif scope == "single_doctor" and step.tool_name in DOCTOR_AGGREGATE_TOOLS:
            issues.append(
                self._issue(
                    "tool.scope.invalid",
                    f"{step.tool_name} is aggregate-only and is not allowed in single_doctor scope.",
                    step.step_id,
                )
            )
        if tool.chain_scope not in {"A", "cross"}:
            issues.append(
                self._issue(
                    "tool.chain_scope.invalid",
                    f"{step.tool_name} has unsupported chain_scope={tool.chain_scope}.",
                    step.step_id,
                )
            )
        return issues

    def _validate_arguments(self, step: LLMPlannedStep, tool: ToolSpec) -> list[PlanValidationIssue]:
        args = self._args_for_schema_validation(step)
        try:
            tool.validate_args(args)
        except Exception as exc:  # noqa: BLE001
            return [
                self._issue(
                    "arguments.schema.invalid",
                    f"Arguments do not match {step.tool_name} schema: {exc}",
                    step.step_id,
                )
            ]
        return []

    def _args_for_schema_validation(self, step: LLMPlannedStep) -> dict[str, Any]:
        args = dict(step.arguments or {})
        if step.tool_name == "set_diff":
            if "base_set_ref" in args and "base_set_id" not in args:
                args["base_set_id"] = f"ref:{args['base_set_ref']}"
            if "subtract_set_ref" in args and "subtract_set_id" not in args:
                args["subtract_set_id"] = f"ref:{args['subtract_set_ref']}"
            args.pop("base_set_ref", None)
            args.pop("subtract_set_ref", None)
        if step.tool_name in {"get_patient_last_visit", "get_patient_plan_status"}:
            if "patient_id" not in args and ("patient_set_ref" in args or "patient_ids_ref" in args):
                args["patient_id"] = 1
            args.pop("patient_set_ref", None)
            args.pop("patient_ids_ref", None)
        if step.tool_name == "rank_patients":
            if "patient_ids" not in args and ("patient_set_ref" in args or "patient_ids_ref" in args):
                args["patient_ids"] = [1]
            args.pop("patient_set_ref", None)
            args.pop("patient_ids_ref", None)
        return args

    def _validate_references(self, step: LLMPlannedStep, seen_step_ids: set[str]) -> list[PlanValidationIssue]:
        issues: list[PlanValidationIssue] = []
        for key, value in (step.arguments or {}).items():
            if not key.endswith("_ref"):
                continue
            if not isinstance(value, str):
                issues.append(self._issue("reference.invalid_type", f"{key} must be a step_id string.", step.step_id))
                continue
            if value not in seen_step_ids:
                issues.append(self._issue("reference.unknown_or_future", f"{key} references unknown or future step_id {value}.", step.step_id))
        return issues

    def _contains_sql(self, args: dict[str, Any]) -> bool:
        text = json.dumps(args, ensure_ascii=False, default=str)
        return bool(SQL_PATTERN.search(text))

    def _signature(self, step: LLMPlannedStep) -> str:
        return json.dumps(
            {"tool_name": step.tool_name, "arguments": step.arguments},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def _issue(self, code: str, message: str, step_id: str | None = None) -> PlanValidationIssue:
        return PlanValidationIssue(code=code, message=message, step_id=step_id)
