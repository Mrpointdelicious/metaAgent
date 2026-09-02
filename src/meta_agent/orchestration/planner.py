"""
创建日期：2026-08-29
文件功能：使用确定性规则生成受约束任务计划，为后续 LLM 规划器预留稳定契约。
"""

from dataclasses import dataclass
from typing import Literal

TaskName = Literal["patient_context", "session_analysis", "single_report"]


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """经过校验、可直接执行的领域任务计划。"""

    tasks: tuple[TaskName, ...]
    requested_output: Literal["answer", "answer_and_report"]


class DeterministicTaskPlanner:
    """以关键词产生最小任务集，避免原型阶段让模型自由选择工具。"""

    _analysis_keywords = ("训练", "解读", "指标", "表现", "改善", "恶化", "持平")
    _report_keywords = ("生成报告", "生成报表", "出图", "图表", "图片")

    def plan(self, query: str) -> TaskPlan:
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
            requested_output="answer_and_report" if wants_report else "answer",
        )
