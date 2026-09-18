"""
创建日期：2026-09-04
文件功能：校验并轻量规范化任务计划，限制任务白名单并补齐确定性硬依赖。
"""

from meta_agent.orchestration.planner import (
    TaskName,
    TaskPlan,
)


class PlanValidator:
    """校验 Planner 结果，并修复少量可确定恢复的计划问题。"""

    _allowed_tasks: frozenset[TaskName] = frozenset(
        {
            "patient_context",
            "session_analysis",
            "single_report",
        }
    )

    _dependencies: dict[
        TaskName,
        tuple[TaskName, ...],
    ] = {
        "single_report": ("session_analysis",),
    }

    def validate(
        self,
        plan: TaskPlan,
    ) -> tuple[TaskName, ...]:
        """返回满足当前确定性约束的任务序列。"""

        tasks = self._deduplicate(plan.tasks)

        unknown = set(tasks) - self._allowed_tasks

        if unknown:
            raise ValueError(f"任务计划包含未授权任务：{sorted(unknown)}")

        return self._ensure_dependencies(tasks)

    @staticmethod
    def _deduplicate(
        tasks: tuple[TaskName, ...],
    ) -> list[TaskName]:
        """去除重复任务，并保留 Planner 原始相对顺序。"""

        return list(dict.fromkeys(tasks))

    def _ensure_dependencies(
        self,
        tasks: list[TaskName],
    ) -> tuple[TaskName, ...]:
        """补齐硬依赖，并保证依赖位于目标任务之前。"""

        resolved: list[TaskName] = []

        for task in tasks:
            dependencies = self._dependencies.get(
                task,
                (),
            )

            for dependency in dependencies:
                if dependency not in resolved:
                    resolved.append(dependency)

            if task not in resolved:
                resolved.append(task)

        return tuple(resolved)
