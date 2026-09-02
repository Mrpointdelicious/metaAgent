"""
创建日期：2026-08-29
文件功能：定义 Dify 兼容请求、健康检查和运行输出的数据结构。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


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
