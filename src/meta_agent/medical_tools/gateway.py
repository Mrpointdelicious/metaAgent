"""
创建日期：2026-11-15
文件功能：将医疗工具统一路由至 AI_WebApi 或中台适配器，沿用已有调用行为。
"""

from meta_agent.config import Settings
from meta_agent.medical_tools.adapters.ai_webapi import AIWebApiAdapter
from meta_agent.medical_tools.contracts import ToolAdapter, ToolContext, ToolRequest, ToolResult
from meta_agent.medical_tools.registry import require_enabled
from meta_agent.orchestration.identity import TrustedScope
from meta_agent.tools.ai_webapi import AIWebApiClient


class MedicalToolGateway:
    def __init__(self, settings: Settings, transport=None, *, backend=None):
        from meta_agent.medical_tools.adapters.platform.doctors import DoctorToolAdapter
        from meta_agent.medical_tools.adapters.platform.scene import SceneToolAdapter

        self.settings = settings
        self.backend = backend if backend is not None else AIWebApiClient(settings, transport)
        self._owns_backend = backend is None
        webapi = AIWebApiAdapter(self.backend)
        self.adapters: dict[str, ToolAdapter] = {"ai_webapi": webapi}
        # 兼容已有测试/嵌入方注入的 post 后端；生产始终使用显式的提供方配置。
        native = isinstance(self.backend, AIWebApiClient)
        self.adapters["platform_scene"] = (
            SceneToolAdapter(self.backend.client, settings) if native else webapi
        )
        self.adapters["platform_doctors"] = (
            DoctorToolAdapter(self.backend.client, settings) if native else webapi
        )

    @classmethod
    def wrap(cls, backend, settings: Settings):
        return backend if isinstance(backend, cls) else cls(settings, backend=backend)

    @property
    def client(self):
        # 问诊流程的其他中台接口继续复用原 HTTP 连接池。
        return self.backend.client

    async def close(self):
        if self._owns_backend:
            await self.backend.close()

    async def execute(
        self, name: str, arguments: dict, context: ToolContext | None = None
    ) -> ToolResult:
        spec = require_enabled(name, self.settings)
        # 原有 dry_run 仍统一由合成后端处理，不发起业务请求。
        provider = "ai_webapi" if self.settings.dry_run else spec.provider
        return await self.adapters[provider].execute(ToolRequest(name, arguments), context)

    async def post(self, name: str, arguments: dict) -> dict:
        return (await self.execute(name, arguments)).domain_payload()

    async def resolve_patient_identity(self, scope: TrustedScope) -> TrustedScope:
        result = await self.execute("resolve_patient_identity", {}, ToolContext(scope))
        return scope.bind(result.domain_payload())

    async def artifact_available(self, url: str) -> bool:
        return await self.backend.artifact_available(url)
