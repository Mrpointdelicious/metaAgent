from __future__ import annotations

from agents import function_tool

from services import ReportService


def build_report_tools(report_service: ReportService) -> list:
    @function_tool
    def generate_review_card(
        patient_id: int | None = None,
        plan_id: int | None = None,
        therapist_id: int | None = None,
        days: int = 30,
    ) -> dict:
        """Generate a therapist-facing structured review card for a single patient."""
        return report_service.generate_review_card(
            patient_id=patient_id,
            plan_id=plan_id,
            therapist_id=therapist_id,
            days=days,
        ).model_dump(mode="json")

    @function_tool
    def screen_risk_patients(
        therapist_id: int,
        days: int = 7,
        top_k: int = 10,
    ) -> list[dict]:
        """Screen multi-patient risk and return a ranked list for therapist review."""
        return [
            item.model_dump(mode="json")
            for item in report_service.screen_risk_patients(
                therapist_id=therapist_id,
                days=days,
                top_k=top_k,
            )
        ]

    @function_tool
    def generate_weekly_risk_report(
        therapist_id: int,
        days: int = 7,
        top_k: int = 10,
    ) -> dict:
        """Generate therapist weekly risk report with statistics and priority patients."""
        return report_service.generate_weekly_risk_report(
            therapist_id=therapist_id,
            days=days,
            top_k=top_k,
        ).model_dump(mode="json")

    return [generate_review_card, screen_risk_patients, generate_weekly_risk_report]
