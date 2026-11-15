"""
创建日期：2026-11-15
文件功能：将稳定工具请求映射到 AI_WebApi，保留已有患者 v2 身份与数据范围。
"""

from meta_agent.medical_tools.contracts import ToolContext, ToolRequest, ToolResult
from meta_agent.medical_tools.registry import PATIENT_ENDPOINTS
from meta_agent.orchestration.identity import validate_patient_envelope


class AIWebApiAdapter:
    def __init__(self, backend):
        self.backend = backend

    async def execute(self, request: ToolRequest, context: ToolContext | None) -> ToolResult:
        if request.name == "query_hospital_directory":
            return ToolResult(
                tool_name=request.name, payload={"directory": await self.backend.directory()}
            )
        payload = request.payload()
        if context and request.name in PATIENT_ENDPOINTS:
            payload = context.scope.patient_payload(payload, context.conversation_id)
        body = await self.backend.post(request.name, payload)
        if context and request.name in PATIENT_ENDPOINTS:
            validate_patient_envelope(
                context.scope,
                request.name,
                body,
                binding=request.name == "resolve_patient_identity",
            )
        return ToolResult(tool_name=request.name, payload=body)
