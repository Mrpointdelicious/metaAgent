"""
创建日期：2026-09-08
文件功能：静态能力表及能力专属参数校验，不接受模型生成身份或网络地址。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from meta_agent.config import Settings
from meta_agent.contracts import Capability, Effect, IReGoRequest, Selector, StrictModel


class EmptyArgs(StrictModel):
    pass


class OverviewArgs(StrictModel):
    force_refresh: bool = False


class HistoryArgs(StrictModel):
    page_number: int = Field(default=1, ge=1, le=10000)
    page_size: int = Field(default=10, ge=1, le=50)
    record_scope: Literal["all", "plans", "reports"] = "all"
    training_state: Literal[
        "all", "completed", "not_started", "not_completed", "execution_unknown", "voided"
    ] = "all"


class ResolveArgs(StrictModel):
    selector: Selector


class SessionArgs(StrictModel):
    session_ref: str | None = None
    selector: Literal["session_ref", "latest_usable"] = "session_ref"


class ReportArgs(StrictModel):
    session_ref: str


class TrendArgs(StrictModel):
    selection_mode: Literal["latest_count", "date_range", "recent_ordinal_range"] = "latest_count"
    report_count: int = Field(default=4, ge=2, le=10)
    start_date: str | None = None
    end_date: str | None = None
    start_ordinal: int | None = Field(default=None, ge=1)
    end_ordinal: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_window(self) -> "TrendArgs":
        if self.selection_mode == "date_range":
            if not self.start_date or not self.end_date:
                raise ValueError("日期范围不完整")
            start = datetime.fromisoformat(self.start_date)
            end = datetime.fromisoformat(self.end_date)
            if start.tzinfo is None or end.tzinfo is None or start > end:
                raise ValueError("日期须包含时区且开始不晚于结束")
        if self.selection_mode == "recent_ordinal_range":
            if self.start_ordinal is None or self.end_ordinal is None:
                raise ValueError("序号范围不完整")
            if not 2 <= self.end_ordinal - self.start_ordinal + 1 <= 10:
                raise ValueError("连续窗口须为2到10次")
        return self


class SceneArgs(StrictModel):
    target: str = Field(min_length=1, max_length=4000)


class DispatchArgs(StrictModel):
    action_ref: str


class KnowledgeArgs(StrictModel):
    query: str = Field(min_length=1, max_length=4000)
    domain: Literal["health", "product", "help"]


@dataclass(frozen=True)
class CapabilitySpec:
    capability_id: Capability
    arguments: type[StrictModel]
    effect: Effect = "read"
    requires_patient: bool = False
    requires_space: bool = False
    provides: tuple[str, ...] = ()
    enabled: bool = True


def capability_specs(settings: Settings) -> dict[str, CapabilitySpec]:
    specs = [
        CapabilitySpec(
            "irego.execute",
            IReGoRequest,
            requires_patient=True,
            provides=("session_ref", "evidence_id", "artifact_ref", "window"),
        ),
        CapabilitySpec("rehab.overview", OverviewArgs, requires_patient=True),
        CapabilitySpec("rehab.history", HistoryArgs, requires_patient=True),
        CapabilitySpec(
            "rehab.resolve_session",
            ResolveArgs,
            requires_patient=True,
            provides=("session_ref", "evidence_id"),
        ),
        CapabilitySpec(
            "rehab.session",
            SessionArgs,
            requires_patient=True,
            provides=("session_ref", "evidence_id"),
        ),
        CapabilitySpec(
            "rehab.single_report", ReportArgs, "prepare_artifact", True, provides=("artifact_ref",)
        ),
        CapabilitySpec("rehab.trend", TrendArgs, requires_patient=True, provides=("window",)),
        CapabilitySpec(
            "rehab.trend_report", TrendArgs, "prepare_artifact", True, provides=("artifact_ref",)
        ),
        CapabilitySpec(
            "scene.resolve",
            SceneArgs,
            requires_space=True,
            provides=("action_ref",),
            enabled=settings.scene_enabled,
        ),
        CapabilitySpec(
            "scene.dispatch",
            DispatchArgs,
            "emit_frontend_action",
            requires_space=True,
            enabled=settings.scene_actions_enabled,
        ),
        CapabilitySpec(
            "doctors.search", EmptyArgs, requires_space=True, enabled=settings.doctors_enabled
        ),
        CapabilitySpec(
            "knowledge.search", KnowledgeArgs, enabled=bool(settings.knowledge_corpus_path)
        ),
        CapabilitySpec("answer.compose", EmptyArgs),
    ]
    return {spec.capability_id: spec for spec in specs}
