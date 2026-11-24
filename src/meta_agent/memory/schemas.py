"""
创建日期：2026-11-17
文件功能：限定患者长期记忆为非医疗枚举偏好，定义读取及写入契约。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from meta_agent.roles import ROLE_AGENTS

TOOL_NAME = "manage_patient_preference"
NAMESPACE = "patient-preferences-v1"
ALLOWED_VALUES = {
    "language": {"zh-CN", "en"},
    "answer_length": {"short", "standard", "detailed"},
    "answer_format": {"paragraphs", "bullets", "steps"},
    "tone": {"neutral", "warm"},
}


class Preference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: Literal["language", "answer_length", "answer_format", "tone"]
    value: Literal[
        "zh-CN",
        "en",
        "short",
        "standard",
        "detailed",
        "paragraphs",
        "bullets",
        "steps",
        "neutral",
        "warm",
    ]

    @model_validator(mode="after")
    def valid_pair(self):
        if self.value not in ALLOWED_VALUES[self.key]:
            raise ValueError("invalid_preference_pair")
        return self


class MemoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create", "update"] = "create"
    content: Preference
    id: UUID | None = None

    @model_validator(mode="after")
    def valid_operation(self):
        if self.action == "create" and self.id is not None:
            raise ValueError("create_requires_no_id")
        if self.action != "create" and self.id is None:
            raise ValueError("existing_memory_id_required")
        return self


@dataclass(frozen=True)
class PreferenceRecord:
    id: UUID
    content: Preference
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class PreferenceSnapshot:
    items: tuple[PreferenceRecord, ...]


@dataclass(frozen=True)
class MemoryChange:
    status: str
    id: UUID


class PreferenceError(Exception):
    """Only the fixed code is safe to expose; never include rejected input."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def memory_enabled(ctx) -> bool:
    return (
        ctx.settings.patient_memory_enabled
        and ctx.scope.has_patient
        and ROLE_AGENTS.for_role(ctx.scope.role).memory == "patient_preferences"
        and TOOL_NAME not in getattr(getattr(ctx, "client", None), "excluded_tools", ())
    )
