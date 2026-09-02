"""
创建日期：2026-08-29
文件功能：规定会话结束后哪些小状态允许保留，避免工具大 JSON 驻留上下文。
"""

from typing import Any


class MemoryRetentionPolicy:
    """从运行状态中筛选可跨轮保留的引用和任务状态。"""

    _retained_keys = {
        "result_ref",
        "requested_output",
        "tasks",
    }

    def compact(self, state: dict[str, Any]) -> dict[str, Any]:
        return {key: state[key] for key in self._retained_keys if key in state}
