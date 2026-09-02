"""
创建日期：2026-08-29
文件功能：根据当前任务选择最小事实集，阻止完整工具 JSON 进入模型上下文。
"""

from typing import Any


class ContextCompiler:
    """生成本轮回答允许使用的紧凑上下文。"""

    _allowed_fact_keys = {
        "status",
        "training_date",
        "completion",
        "training_items",
        "session_ref",
        "ability_evaluations",
        "available_actions",
    }

    def compile(self, query: str, facts: dict[str, Any]) -> dict[str, Any]:
        compact_facts = {
            key: value for key, value in facts.items() if key in self._allowed_fact_keys
        }
        return {"query": query, "facts": compact_facts}
