from __future__ import annotations

from agents import function_tool

from services import OutcomeService, PlanService


def build_outcome_tools(plan_service: PlanService, outcome_service: OutcomeService) -> list:
    @function_tool
    def get_outcome_change(
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
    ) -> dict:
        """Return HealthScore, GameScore, and parsed report trend summary."""
        plan_summary = plan_service.get_plan_summary(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
        )
        return outcome_service.get_outcome_change(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            plan_summary=plan_summary,
        ).model_dump(mode="json")

    return [get_outcome_change]
