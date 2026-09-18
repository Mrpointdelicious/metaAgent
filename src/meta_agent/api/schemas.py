"""
创建日期：2026-08-29
文件功能：定义 Dify 兼容请求、健康检查和运行输出的数据结构。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from meta_agent.contracts import StrictModel


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


class DifyChatRequest(BaseModel):
    """兼容 Dify Chatflow 的最小请求结构。"""

    inputs: dict[str, Any] = Field(default_factory=dict)
    query: str
    response_mode: Literal["streaming", "blocking"] = "streaming"
    conversation_id: str = ""
    user: str

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query 不能为空")
        return normalized

    @field_validator("user")
    @classmethod
    def validate_user(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("user 必须是可信接入方提供的稳定用户标识")
        return normalized


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
