from __future__ import annotations

from agents import function_tool

from services import ReportService


def build_reflection_tools(report_service: ReportService) -> list:
    @function_tool
    def reflect_on_output(
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
    ) -> dict:
        """Run constrained reflection checks over the current single-patient output."""
        review_card = report_service.generate_review_card(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
        )
        return review_card.reflection.model_dump(mode="json")

    return [reflect_on_output]
