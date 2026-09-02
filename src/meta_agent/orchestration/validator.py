"""
创建日期：2026-08-29
文件功能：校验任务白名单和依赖关系，禁止规划器产生未授权工具链。
"""

from meta_agent.orchestration.planner import TaskName, TaskPlan


class PlanValidator:
    """对算法或未来 LLM 生成的任务计划执行确定性校验。"""

    _allowed_tasks = {"patient_context", "session_analysis", "single_report"}

    def validate(self, plan: TaskPlan) -> tuple[TaskName, ...]:
        tasks = tuple(plan.tasks)
        unknown = set(tasks) - self._allowed_tasks
        if unknown:
            raise ValueError(f"任务计划包含未授权任务：{sorted(unknown)}")
        if "single_report" in tasks and "session_analysis" not in tasks:
            raise ValueError("生成单次报告前必须先执行训练会话分析")
        return tasks
