"""
创建日期：2026-09-12
文件功能：保留未来 Dify 事件适配器边界，不参与当前运行链。
"""

from meta_agent.contracts import OutboundEvent


class DifyEventAdapter:
    def encode(self, event: OutboundEvent) -> str:
        raise NotImplementedError("Dify protocol mapping is deferred; use NativeEventAdapter")
