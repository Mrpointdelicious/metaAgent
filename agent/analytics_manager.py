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

from .schemas import (
    AnalyticsScope,
    AnalyticsStructuredOutput,
    AnalyticsTimeSlots,
    IntentDecision,
    OpenAnalyticsSubtype,
    OrchestratorRequest,
    OrchestratorResponse,
    QueryPlan,
    QueryPlanStep,
    RelativeWindow,
    ResolvedAnalyticsRanges,
    ResolvedWindow,
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
    ):
        self.analytics_service = analytics_service
        self.analytics_tool_registry = analytics_tool_registry
        self.settings = settings or get_settings()

    def run(
        self,
        request: OrchestratorRequest,
        decision: IntentDecision,
        *,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
    ) -> OrchestratorResponse:
        question = (request.raw_text or "").strip()
        subtype = decision.analytics_subtype
        logger.info(
            "open analytics execute subtype=%s scope=%s question=%r",
            subtype,
            decision.analysis_scope,
            question,
        )

        if subtype == "absent_old_patients_recent_window":
            return self._run_absent_old_patients_recent_window(
                request,
                decision,
                mode=mode,
                llm_config=llm_config,
                execution_mode=execution_mode,
            )
        if subtype == "absent_from_baseline_window":
            return self._run_absent_from_baseline_window(
                request,
                decision,
                mode=mode,
                llm_config=llm_config,
                execution_mode=execution_mode,
            )
        if subtype == "doctors_with_active_plans":
            return self._run_doctors_with_active_plans(
                request,
                decision,
                mode=mode,
                llm_config=llm_config,
                execution_mode=execution_mode,
            )
        return self._build_not_supported_response(
            request,
            decision,
            llm_config=llm_config,
            execution_mode=execution_mode,
            reason="Unable to stably classify this open analytics question into a supported subtype.",
        )

    def _run_absent_old_patients_recent_window(
        self,
        request: OrchestratorRequest,
        decision: IntentDecision,
        *,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
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
        )

    def _run_absent_from_baseline_window(
        self,
        request: OrchestratorRequest,
        decision: IntentDecision,
        *,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
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
        )

    def _run_doctors_with_active_plans(
        self,
        request: OrchestratorRequest,
        decision: IntentDecision,
        *,
        mode: str,
        llm_config: ResolvedLLMConfig,
        execution_mode: str,
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
