"""
创建日期：2026-08-29
文件功能：维护面向 Agent 的少量领域 Workflow 注册信息和动态筛选元数据。
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class WorkflowDescriptor:
    """一个可向编排器暴露的领域 Workflow 描述。"""

    name: str
    domain: str
    description: str
    handler: Any


class WorkflowRegistry:
    """按领域注册和获取任务型 Workflow。"""

    def __init__(self) -> None:
        self._items: dict[str, WorkflowDescriptor] = {}

    def register(self, descriptor: WorkflowDescriptor) -> None:
        if descriptor.name in self._items:
            raise ValueError(f"Workflow 已注册：{descriptor.name}")
        self._items[descriptor.name] = descriptor

    def get(self, name: str) -> WorkflowDescriptor:
        try:
            return self._items[name]
        except KeyError as exc:
            raise KeyError(f"未找到 Workflow：{name}") from exc

    def list_for_domain(self, domain: str) -> list[WorkflowDescriptor]:
        return [item for item in self._items.values() if item.domain == domain]
