"""
创建日期：2026-08-29
文件功能：定义可替换模型供应商的异步结构化生成协议，不绑定具体厂商。
"""

from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

OutputModel = TypeVar("OutputModel", bound=BaseModel)


class StructuredLLMProvider(Protocol):
    """未来任务拆分和文案组织模型必须实现的最小契约。"""

    async def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        output_schema: type[OutputModel],
        context: dict[str, Any],
    ) -> OutputModel:
        """生成符合指定 Pydantic Schema 的结果。"""
        ...
