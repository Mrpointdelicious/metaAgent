from __future__ import annotations

from .schemas import OrchestrationTaskType, normalize_task_type


def build_task_instructions(task_type: str) -> str:
    normalized = normalize_task_type(task_type)
    base = (
        "你是一个受约束的康复复核助手。"
        "你只能使用白名单工具。"
        "不要臆造 schema 字段，不要编写 SQL，也不要让 B 链步态证据影响 A 链风险评分。"
    )
    if normalized == OrchestrationTaskType.REVIEW_PATIENT:
        return (
            base
            + "任务：单患者复核。优先调用 generate_review_card，再调用 reflect_on_output。"
            + "步态证据必须保持为独立证据块。"
        )
    if normalized == OrchestrationTaskType.SCREEN_RISK:
        return (
            base
            + "任务：治疗师侧多患者风险筛选。优先调用 screen_risk_patients；如果需要补充前几名患者原因，再调用 generate_review_card，最后调用 reflect_on_output。"
            + "不要把步态证据并入患者群体风险判断。"
        )
    if normalized == OrchestrationTaskType.WEEKLY_REPORT:
        return (
            base
            + "任务：生成治疗师周报。优先调用 generate_weekly_risk_report，再调用 reflect_on_output。"
        )
    if normalized == OrchestrationTaskType.GAIT_REVIEW:
        return (
            base
            + "任务：仅返回 gait_review 的独立证据。使用 get_gait_explanation，再调用 reflect_on_output。"
            + "不要基于这部分输出计算 A 链风险。"
        )
    return base + "如果任务不受支持，请直接明确说明。"
