"""
创建日期：2026-08-29
文件功能：定义领域 Workflow 与底层工具之间的稳定输入输出契约。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class WorkflowOutcome:
    """领域 Workflow 返回给编排层的结构化结果。"""

    patient_message: str
    facts: dict[str, Any]
    raw_payload: dict[str, Any]
    image_urls: list[str] = field(default_factory=list)
    mode_commands: list[str] = field(default_factory=list)
