"""
创建日期：2026-08-29
文件功能：定义统一任务规划契约、结构化规划结果和确定性规划器。
注意：此为非llm 规划结点，用于无LLM时兜底。
LLM结点位于llm_planner.py
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field

TaskName = Literal[
    "patient_context",
    "session_analysis",
    "single_report",
]


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """Planner 生成的候选领域任务计划。"""

    tasks: tuple[TaskName, ...]
    requested_output: Literal[
        "answer",
        "answer_and_report",
    ]


class PlannerDecision(BaseModel):
    """LLM Planner 返回的结构化原始决策。"""

    tasks: list[TaskName] = Field(
        min_length=1,
        max_length=3,
        description="完成用户请求所需的最小领域任务集合",
    )

    requested_output: Literal[
        "answer",
        "answer_and_report",
    ]


class TaskPlanner(Protocol):
    """所有任务规划器必须遵循的统一接口。"""

    async def plan(
        self,
        query: str,
    ) -> TaskPlan: ...


class DeterministicTaskPlanner:
    """以关键词产生最小任务集。"""

    _analysis_keywords = (
        "训练",
        "解读",
        "指标",
        "表现",
        "改善",
        "恶化",
        "持平",
    )

    _report_keywords = (
        "生成报告",
        "生成报表",
        "出图",
        "图表",
        "图片",
    )

    async def plan(
        self,
        query: str,
    ) -> TaskPlan:
        wants_report = any(keyword in query for keyword in self._report_keywords)

        wants_analysis = wants_report or any(
            keyword in query for keyword in self._analysis_keywords
        )

        tasks: list[TaskName] = []

        if wants_analysis:
            tasks.append("session_analysis")
        else:
            tasks.append("patient_context")

        if wants_report:
            tasks.append("single_report")

        return TaskPlan(
            tasks=tuple(tasks),
            requested_output=("answer_and_report" if wants_report else "answer"),
        )
