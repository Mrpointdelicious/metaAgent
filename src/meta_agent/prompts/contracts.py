"""
创建日期：2026-10-09
文件功能：定义提示词输入、不可变版本绑定及渲染结果。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langchain_core.messages import SystemMessage
from pydantic import BaseModel, Field

from meta_agent.contracts import StrictModel


class AgentPromptInputs(StrictModel):
    now: datetime
    patient_profile: dict[str, Any] | None = None
    budget_exhausted: bool = False


class FrontendPromptInputs(StrictModel):
    selected_session_ref: str | None
    selected_record_domain: str | None
    current_view: str | None
    current_record: dict[str, Any] | None


class IntentPromptInputs(StrictModel):
    query: str
    recent_turns: list[dict[str, str]]
    record_candidates: list[str]
    record_domain: str | None
    history_domain: str | None
    history_page: int | None
    pending_goals: list[dict[str, Any]]
    patient_brief: str | None


class FactSelectionPromptInputs(StrictModel):
    query: str
    facts: list[dict[str, Any]]


class EmptyPromptInputs(StrictModel):
    pass


class FactSelection(StrictModel):
    fact_ids: list[str] = Field(max_length=100)


@dataclass(frozen=True)
class PromptDefinition:
    prompt_id: str
    version: str
    input_model: type[BaseModel]
    output_schema: type[BaseModel] | None
    components: tuple[tuple[str, str], ...]
    component_versions: tuple[tuple[str, str], ...]
    template_hash: str

    def text(self, component: str) -> str:
        return dict(self.components)[component]


@dataclass(frozen=True)
class PromptBinding:
    bundle: str
    definitions: tuple[PromptDefinition, ...]

    def definition(self, prompt_id: str) -> PromptDefinition:
        for definition in self.definitions:
            if definition.prompt_id == prompt_id:
                return definition
        raise ValueError(f"Unknown prompt ID: {prompt_id}")


@dataclass(frozen=True)
class RenderedPrompt:
    messages: tuple[tuple[str, str], ...]
    prompt_id: str
    version: str
    template_hash: str
    fragments: tuple[str, ...]

    @property
    def system_message(self) -> SystemMessage:
        return SystemMessage(content=next(text for role, text in self.messages if role == "system"))

    def usage(self, call: int) -> dict[str, Any]:
        return {
            "call": call,
            "prompt_id": self.prompt_id,
            "version": self.version,
            "template_hash": self.template_hash,
            "fragments": list(self.fragments),
        }
