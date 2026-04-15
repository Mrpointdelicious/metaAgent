from __future__ import annotations

from agents import function_tool

from services import PlanService


def build_plan_tools(plan_service: PlanService) -> list:
    @function_tool
    def get_plan_summary(
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
    ) -> dict:
        """Return plan-layer summary for a patient or plan within a time window."""
        return plan_service.get_plan_summary(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
        ).model_dump(mode="json")

    return [get_plan_summary]
