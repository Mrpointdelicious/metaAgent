from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field
from pydantic_core import PydanticUndefined


ToolChainScope = Literal["A", "B", "cross"]
ToolMode = Literal["direct", "agents_sdk"]


class PatientPlanWindowInput(BaseModel):
    patient_id: int | None = Field(default=None, description="Patient ID.")
    plan_id: int | None = Field(default=None, description="Plan ID.")
    therapist_id: int | None = Field(default=None, description="Therapist or doctor ID.")
    days: int = Field(default=30, ge=1, le=3650, description="Relative time window in days.")


class GenerateReviewCardInput(PatientPlanWindowInput):
    patient_ids: list[int] = Field(default_factory=list, description="Patient IDs used in batch review mode.")


class TherapistWindowInput(BaseModel):
    therapist_id: int = Field(description="Therapist or doctor ID.")
    days: int = Field(default=7, ge=1, le=3650, description="Relative time window in days.")
    top_k: int = Field(default=10, ge=1, le=100, description="Max number of rows to return.")


class GaitExplanationInput(BaseModel):
    patient_id: int | None = Field(default=None, description="Patient ID.")
    item_id: int | None = Field(default=None, description="Walk item ID.")
    days: int = Field(default=30, ge=1, le=3650, description="Relative time window in days.")


class ReflectOnOutputInput(BaseModel):
    task_type: str | None = Field(default=None, description="Current task type.")
    current_output: Any | None = Field(default=None, description="Current structured output to inspect.")
    patient_id: int | None = Field(default=None, description="Patient ID.")
    plan_id: int | None = Field(default=None, description="Plan ID.")
    therapist_id: int | None = Field(default=None, description="Therapist or doctor ID.")
    days: int = Field(default=30, ge=1, le=3650, description="Relative time window in days.")


class DoctorWindowInput(BaseModel):
    doctor_id: int = Field(description="Doctor or therapist ID.")
    start_date: str | None = Field(default=None, description="Window start date.")
    end_date: str | None = Field(default=None, description="Window end date.")
    source: str = Field(default="attendance", description="Source type for the patient set.")


class DateWindowInput(BaseModel):
    start_date: str | None = Field(default=None, description="Window start date.")
    end_date: str | None = Field(default=None, description="Window end date.")


class PatientSetDiffInput(BaseModel):
    base_set_id: str = Field(description="Base set ID.")
    subtract_set_id: str = Field(description="Set ID to subtract from the base set.")


class LastVisitInput(BaseModel):
    patient_id: int = Field(description="Patient ID.")
    doctor_id: int | None = Field(default=None, description="Doctor or therapist ID.")


class PatientPlanStatusInput(BaseModel):
    patient_id: int = Field(description="Patient ID.")
    doctor_id: int | None = Field(default=None, description="Doctor or therapist ID.")
    start_date: str = Field(description="Window start date for plan status.")
    end_date: str = Field(description="Window end date for plan status.")


class RankPatientsInput(BaseModel):
    patient_ids: list[int] = Field(default_factory=list, description="Patient IDs to rank.")
    strategy: str = Field(description="Ranking strategy.")
    top_k: int | None = Field(default=None, ge=1, le=1000, description="Optional result limit.")


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

    def get_agent_tool(self) -> Any | None:
        return self.agent_tool

    def metadata(self) -> dict[str, Any]:
        fields = getattr(self.input_model, "model_fields", {})
        def field_default(field) -> Any:  # noqa: ANN001
            if field.is_required():
                return None
            if field.default_factory is not None:
                return "default_factory"
            if field.default is PydanticUndefined:
                return None
            return field.default

        return {
            "tool_name": self.tool_name,
            "description": self.description,
            "input_schema": {
                name: {
                    "annotation": str(field.annotation),
                    "required": field.is_required(),
                    "default": field_default(field),
                }
                for name, field in fields.items()
            },
            "output_schema": self.output_schema,
            "chain_scope": self.chain_scope,
            "can_affect_risk_score": self.can_affect_risk_score,
        }
