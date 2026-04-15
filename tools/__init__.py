from __future__ import annotations

from .base import ToolSpec
from .execution_tools import build_execution_tools
from .gait_tools import build_gait_tools
from .outcome_tools import build_outcome_tools
from .plan_tools import build_plan_tools
from .reflection_tools import build_reflection_tools
from .report_tools import build_report_tools


def build_tool_registry(*tool_groups: list[ToolSpec]) -> dict[str, ToolSpec]:
    registry: dict[str, ToolSpec] = {}
    for group in tool_groups:
        for tool in group:
            registry[tool.tool_name] = tool
    return registry


__all__ = [
    "ToolSpec",
    "build_execution_tools",
    "build_gait_tools",
    "build_outcome_tools",
    "build_plan_tools",
    "build_reflection_tools",
    "build_report_tools",
    "build_tool_registry",
]
