"""
创建日期：2026-11-17
文件功能：统一适配器错误，复用现有领域错误的状态与重试语义。
"""

from meta_agent.contracts import DomainError


class ToolError(DomainError):
    pass
