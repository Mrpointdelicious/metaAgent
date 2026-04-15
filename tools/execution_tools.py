from __future__ import annotations

from agents import function_tool

from services import DeviationService, ExecutionService, OutcomeService, PlanService


def build_execution_tools(
    plan_service: PlanService,
    execution_service: ExecutionService,
    outcome_service: OutcomeService,
    deviation_service: DeviationService,
) -> list:
    @function_tool
    def get_execution_logs(
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
    ) -> dict:
        """Return execution-layer logs and aggregated session evidence."""
        plan_summary = plan_service.get_plan_summary(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
        )
        return execution_service.get_execution_logs(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            plan_summary=plan_summary,
        ).model_dump(mode="json")

    @function_tool
    def calc_deviation_metrics(
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
    ) -> dict:
        """Calculate attendance, completion, dose deviation, and interruption risk."""
        plan_summary = plan_service.get_plan_summary(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
        )
        execution_summary = execution_service.get_execution_logs(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            plan_summary=plan_summary,
        )
        outcome_change = outcome_service.get_outcome_change(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            plan_summary=plan_summary,
        )
        return deviation_service.calc_deviation_metrics(
            plan_summary=plan_summary,
            execution_summary=execution_summary,
            outcome_change=outcome_change,
        ).model_dump(mode="json")

    return [get_execution_logs, calc_deviation_metrics]
