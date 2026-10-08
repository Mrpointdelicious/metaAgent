"""
创建日期：2026-10-05
文件功能：定义Agent可见的业务工具参数，患者身份由系统注入。
"""

from typing import Literal

from pydantic import Field, model_validator

from meta_agent.contracts import ActivityScope, StrictModel


class EmptyArgs(StrictModel):
    pass


class PatientPageArgs(StrictModel):
    page_number: int = Field(default=1, ge=1, le=100000)
    page_size: int = Field(default=5, ge=1, le=20)


class HistoryArgs(StrictModel):
    page_number: int = Field(default=1, ge=1, le=100000)
    page_size: int = Field(default=10, ge=1, le=50)
    record_scope: Literal["all", "past", "future", "unfinished", "voided"] = "all"
    training_state: str = Field(default="all", max_length=40)


class TourHistoryArgs(HistoryArgs):
    record_scope: Literal["all", "with_result", "without_result"] = "all"
    activity_scope: Literal["all"] | ActivityScope = "all"
    training_state: Literal[
        "all", "completed", "not_started", "not_completed", "interrupted", "execution_unknown"
    ] = "all"
    result_state: Literal[
        "all", "summary_available", "raw_only", "no_result", "result_conflicting", "result_unusable"
    ] = "all"


class GoHistoryArgs(HistoryArgs):
    project_scope: str = Field(default="all", max_length=40)


class SessionArgs(StrictModel):
    selector: Literal["latest_usable", "session_ref"] = "latest_usable"
    session_ref: str | None = Field(default=None, max_length=1000)
    detail_level: Literal["compact", "standard"] = "standard"
    quality_detail: Literal["summary", "full"] = "summary"

    @model_validator(mode="after")
    def check_ref(self):
        if self.selector == "session_ref" and not self.session_ref:
            raise ValueError("选择 session_ref 时必须提供历史工具返回的记录引用")
        return self


class TrendArgs(StrictModel):
    selection_mode: Literal["latest_count", "date_range", "recent_ordinal_range"] = "latest_count"
    report_count: int = Field(default=4, ge=2, le=20)
    start_date: str | None = Field(default=None, max_length=60)
    end_date: str | None = Field(default=None, max_length=60)
    start_ordinal: int | None = Field(default=None, ge=1)
    end_ordinal: int | None = Field(default=None, ge=1)
    detail_level: Literal["compact", "standard"] = "standard"
    quality_detail: Literal["summary", "full"] = "summary"


class TourTrendArgs(TrendArgs):
    activity_scope: ActivityScope = "straight_primary"


class GoTrendArgs(TrendArgs):
    report_count: int = Field(default=4, ge=2, le=10)


class TourReportTrendArgs(TourTrendArgs):
    report_count: int = Field(default=4, ge=3, le=20)


class GoReportTrendArgs(GoTrendArgs):
    report_count: int = Field(default=4, ge=3, le=10)


class ReportArgs(StrictModel):
    session_ref: str = Field(min_length=1, max_length=1000)


class SceneArgs(StrictModel):
    target: str = Field(min_length=1, max_length=4000)


class KnowledgeArgs(StrictModel):
    query: str = Field(min_length=1, max_length=4000)
    domain: Literal["health", "product", "help"] = "health"
