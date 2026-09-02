"""
创建日期：2026-08-29
文件功能：把内部事件转换为现有前端识别的 answer、mode 和 image 标签。
"""


class LegacyProtocol:
    """集中维护旧协议，避免标签散落在业务节点中。"""

    @staticmethod
    def answer(text: str) -> str:
        return f"[answer]{text}"

    @staticmethod
    def mode(command: str) -> str:
        return f"[mode]{command}"

    @staticmethod
    def image(url: str) -> str:
        return f"[image]{url}"
