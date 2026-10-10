"""
创建日期：2026-10-10
文件功能：定义面向前端的Run创建、业务上下文及动作确认契约。
"""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from meta_agent.contracts import StrictModel


class FrontendContext(StrictModel):
    patient_id: str | None = Field(default=None, min_length=1, max_length=160)
    space_id: str | None = Field(default=None, min_length=1, max_length=160)
    scene_version: int | None = Field(default=None, ge=0, strict=True)
    selected_session_ref: str | None = Field(default=None, min_length=1, max_length=1000)
    current_view: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("space_id", "selected_session_ref", "current_view")
    @classmethod
    def nonempty(cls, value):
        if value is not None and not value.strip():
            raise ValueError("上下文字段不能为空白")
        return value.strip() if value is not None else None

    @field_validator("patient_id")
    @classmethod
    def project_patient(cls, value):
        if value is not None:
            if not value.isascii() or not value.isdigit() or int(value) <= 0:
                raise ValueError("patient_id 必须是项目患者正整数编号")
            return str(int(value))
        return value

    @model_validator(mode="after")
    def selected_patient(self):
        if self.selected_session_ref and not self.patient_id:
            raise ValueError("selected_session_ref 需要 patient_id")
        return self


class CreateRunRequest(StrictModel):
    request_id: str = Field(min_length=1, max_length=160)
    conversation_id: str | None = Field(default=None, max_length=160)
    query: str = Field(min_length=1, max_length=32000)
    context: FrontendContext = Field(default_factory=FrontendContext)

    @field_validator("request_id", "query", "conversation_id")
    @classmethod
    def nonempty(cls, value):
        if value is None:
            return value
        if not value.strip():
            raise ValueError("字段不能为空")
        return value.strip()


class ActionAckRequest(StrictModel):
    status: Literal["executed", "failed", "ignored"]
    reason: str | None = Field(default=None, max_length=500)


class CreatedRunResponse(StrictModel):
    request_id: str
    run_id: str
    conversation_id: str
    status: Literal["accepted"] = "accepted"
    events_url: str
    snapshot_url: str


class RunSnapshotResponse(StrictModel):
    request_id: str
    run_id: str
    conversation_id: str
    status: Literal[
        "running",
        "succeeded",
        "partial",
        "clarification",
        "unavailable",
        "failed",
        "cancelled",
        "unsupported",
    ]
    last_seq: int = Field(ge=0)
    created_at: datetime


class AcceptedResponse(StrictModel):
    accepted: Literal[True] = True


class ActionAckResponse(AcceptedResponse):
    action_id: str
    status: Literal["executed", "failed", "ignored"]


class ApiError(StrictModel):
    code: str
    message: str
    retryable: bool


class ErrorResponse(StrictModel):
    error: ApiError
