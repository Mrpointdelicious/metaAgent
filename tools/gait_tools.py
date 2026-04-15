from __future__ import annotations

from agents import function_tool

from services import GaitService

from .base import GaitExplanationInput, ToolSpec


def build_gait_tools(gait_service: GaitService) -> list[ToolSpec]:
    def _get_gait_explanation(
        patient_id: int | None = None,
        item_id: int | None = None,
        days: int = 30,
    ) -> dict:
        return gait_service.get_gait_explanation(
            patient_id=patient_id,
            item_id=item_id,
            days=days,
        ).model_dump(mode="json")

    @function_tool
    def get_gait_explanation(
        patient_id: int | None = None,
        item_id: int | None = None,
        days: int = 30,
    ) -> dict:
        """以独立证据块形式返回 B 链步态或步道证据。"""
        return _get_gait_explanation(
            patient_id=patient_id,
            item_id=item_id,
            days=days,
        )

    return [
        ToolSpec(
            tool_name="get_gait_explanation",
            description="获取 B 链步态证据。该输出必须与 A 链风险评分保持分离。",
            input_model=GaitExplanationInput,
            output_schema="GaitExplanationSummary JSON。",
            chain_scope="B",
            can_affect_risk_score=False,
            direct_handler=_get_gait_explanation,
            agent_tool=get_gait_explanation,
            agent_handler=_get_gait_explanation,
        )
    ]
