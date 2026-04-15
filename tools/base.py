from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field


ToolChainScope = Literal["A", "B", "cross"]
ToolMode = Literal["direct", "agents_sdk"]


class PatientPlanWindowInput(BaseModel):
    patient_id: int | None = Field(default=None, description="患者 ID。")
    plan_id: int | None = Field(default=None, description="计划 ID。")
    therapist_id: int | None = Field(default=None, description="治疗师或医生 ID。")
    days: int = Field(default=30, ge=1, le=3650, description="时间窗口天数。")


class GenerateReviewCardInput(PatientPlanWindowInput):
    patient_ids: list[int] = Field(default_factory=list, description="批量复核时使用的患者 ID 列表。")


class TherapistWindowInput(BaseModel):
    therapist_id: int = Field(description="治疗师或医生 ID。")
    days: int = Field(default=7, ge=1, le=3650, description="时间窗口天数。")
    top_k: int = Field(default=10, ge=1, le=100, description="返回结果数量上限。")


class GaitExplanationInput(BaseModel):
    patient_id: int | None = Field(default=None, description="患者 ID。")
    item_id: int | None = Field(default=None, description="步道项目 ID。")
    days: int = Field(default=30, ge=1, le=3650, description="时间窗口天数。")


class ReflectOnOutputInput(BaseModel):
    task_type: str | None = Field(default=None, description="当前任务类型。")
    current_output: Any | None = Field(default=None, description="待检查的当前结构化输出。")
    patient_id: int | None = Field(default=None, description="患者 ID。")
    plan_id: int | None = Field(default=None, description="计划 ID。")
    therapist_id: int | None = Field(default=None, description="治疗师或医生 ID。")
    days: int = Field(default=30, ge=1, le=3650, description="时间窗口天数。")


@dataclass(frozen=True)
class ToolSpec:
    tool_name: str
    description: str
    input_model: type[BaseModel]
    output_schema: str
    chain_scope: ToolChainScope
    can_affect_risk_score: bool
    direct_handler: Callable[..., Any]
    agent_tool: Any | None = None
    agent_handler: Callable[..., Any] | None = None

    def validate_args(self, args: dict[str, Any]) -> BaseModel:
        return self.input_model.model_validate(args)

    def invoke(self, *, mode: ToolMode, args: dict[str, Any]) -> Any:
        validated = self.validate_args(args)
        handler = self.direct_handler
        if mode == "agents_sdk" and self.agent_handler is not None:
            handler = self.agent_handler
        payload = validated.model_dump(exclude_none=True)
        return handler(**payload)

    def metadata(self) -> dict[str, Any]:
        fields = getattr(self.input_model, "model_fields", {})
        return {
            "tool_name": self.tool_name,
            "description": self.description,
            "input_schema": {
                name: {
                    "annotation": str(field.annotation),
                    "required": field.is_required(),
                    "default": None if field.is_required() else field.default,
                }
                for name, field in fields.items()
            },
            "output_schema": self.output_schema,
            "chain_scope": self.chain_scope,
            "can_affect_risk_score": self.can_affect_risk_score,
        }
