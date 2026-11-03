"""
创建日期：2026-08-29
文件功能：定义 Dify 兼容请求、健康检查和运行输出的数据结构。
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, field_validator

from meta_agent.contracts import StrictModel, identifier


class RunAccessRequest(StrictModel):
    user: str = Field(min_length=1, max_length=160)
    inputs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("user")
    @classmethod
    def nonempty_user(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("可信用户标识不能为空")
        return value.strip()


class NativeChatRequest(RunAccessRequest):
    request_id: str = Field(min_length=1, max_length=160)
    query: str = Field(min_length=1, max_length=32000)
    conversation_id: str = Field(default="", max_length=160)
    response_mode: Literal["streaming", "blocking"] = "streaming"

    @field_validator("query", "request_id")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("字段不能为空")
        return value.strip()


class DifyChatRequest(NativeChatRequest):
    """Text chat compatibility; omitted request IDs are unique for each call."""

    model_config = ConfigDict(extra="ignore")
    request_id: str = Field(
        default_factory=lambda: identifier("request"), min_length=1, max_length=160
    )
    files: list[dict[str, Any]] = Field(default_factory=list, max_length=0)
    auto_generate_name: bool = True


def chat_protocol(value: Any) -> str:
    if isinstance(value, DifyChatRequest):
        return "dify"
    if isinstance(value, NativeChatRequest) or isinstance(value, dict) and "request_id" in value:
        return "native"
    return "dify"


ChatRequest = Annotated[
    Annotated[NativeChatRequest, Tag("native")] | Annotated[DifyChatRequest, Tag("dify")],
    Discriminator(chat_protocol),
]


class DifyWorkflowRequest(BaseModel):
    """兼容 Dify Workflow 的最小请求结构。"""

    inputs: dict[str, Any] = Field(default_factory=dict)
    response_mode: Literal["streaming", "blocking"] = "streaming"
    user: str

    @field_validator("user")
    @classmethod
    def validate_user(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("user 必须是可信接入方提供的稳定用户标识")
        return normalized


class HealthResponse(BaseModel):
    """健康检查响应。"""

    status: Literal["ok", "degraded", "failed"]
    service: str
    version: str
    checks: dict[str, str] = Field(default_factory=dict)
