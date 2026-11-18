"""
创建日期：2026-11-17
文件功能：将稳定工具请求映射到 AI_WebApi，保留已有患者 v2 身份与数据范围。

注意：患者身份装填（scope.patient_payload）与封套校验仍由 `RunContext.call` 负责，
这里只做「工具名 → 传输」的映射，避免同一份 payload 被注入两次。
"""

from meta_agent.medical_tools.contracts import ToolContext, ToolRequest, ToolResult
from meta_agent.medical_tools.registry import DIRECTORY_ENDPOINT


class AIWebApiAdapter:
    def __init__(self, backend) -> None:
        self.backend = backend

    async def execute(self, request: ToolRequest, context: ToolContext | None = None) -> ToolResult:
        del context
        if request.name == DIRECTORY_ENDPOINT:
            return ToolResult(
                tool_name=request.name, payload={"directory": await self.backend.directory()}
            )
        body = await self.backend.post(request.name, request.payload())
        return ToolResult(tool_name=request.name, payload=body)
