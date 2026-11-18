"""
创建日期：2026-11-16
文件功能：AI问诊流程独立分支：完整问诊任务分类 + 子流程路由。

与 consultation_room 的唯一差别是分支注册名与 goal 记账键，便于按令牌
单独观察与演进；任务分类器、子流程处理器与问诊分支保持同一份实现。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from meta_agent.flows.consultation_room import Flow as _ConsultationRoomFlow

if TYPE_CHECKING:
    from meta_agent.application.service import ApplicationService


class Flow(_ConsultationRoomFlow):
    # 与 FixedTaskFlow 对齐的分支标识，便于统一观察。
    branch = "ai_consultation"

    def __init__(self, application: ApplicationService) -> None:
        super().__init__(application, goal_root="ai_consultation")
