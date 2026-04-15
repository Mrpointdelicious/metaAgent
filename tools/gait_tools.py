from __future__ import annotations

from agents import function_tool

from services import GaitService


def build_gait_tools(gait_service: GaitService) -> list:
    @function_tool
    def get_gait_explanation(
        patient_id: int | None = None,
        item_id: int | None = None,
        days: int = 30,
    ) -> dict:
        """Return optional gait or walkway explanation as supplemental evidence."""
        return gait_service.get_gait_explanation(
            patient_id=patient_id,
            item_id=item_id,
            days=days,
        ).model_dump(mode="json")

    return [get_gait_explanation]
