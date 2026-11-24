"""
创建日期：2026-11-17
文件功能：将医疗工具统一路由至本地中台适配器或 AI_WebApi，沿用已有调用行为。

- 生产：`backend` 是 `AIWebApiClient`，场景/医生走本地适配器（不请求 .NET 端点）；
- 测试：注入的合成后端不是 `AIWebApiClient` 时，场景/医生回落到该后端的 post，
  便于用 overrides 模拟上游（与合并前行为一致）；
- `dry_run`：一律走 AI_WebApi 适配器，由后端返回 demo 数据，不发起业务请求。
"""

from pydantic import ValidationError

from meta_agent.config import Settings
from meta_agent.contracts import HospitalRequest
from meta_agent.medical_tools.adapters.ai_webapi import AIWebApiAdapter
from meta_agent.medical_tools.contracts import ToolAdapter, ToolContext, ToolRequest, ToolResult
from meta_agent.medical_tools.errors import ToolError
from meta_agent.medical_tools.registry import require_enabled
from meta_agent.orchestration.identity import TrustedScope


class MedicalToolGateway:
    def __init__(self, settings: Settings, transport=None, *, backend=None) -> None:
        from meta_agent.medical_tools.adapters.platform.doctors import DoctorToolAdapter
        from meta_agent.medical_tools.adapters.platform.scene import SceneToolAdapter
        from meta_agent.tools.ai_webapi import AIWebApiClient

        self.settings = settings
        self.backend = backend if backend is not None else AIWebApiClient(settings, transport)
        self._owns_backend = backend is None
        webapi = AIWebApiAdapter(self.backend)
        self.adapters: dict[str, ToolAdapter] = {"ai_webapi": webapi}
        # 只有真实客户端才接本地中台（合成后端没有 .client 连接池）。
        native = isinstance(self.backend, AIWebApiClient)
        self.adapters["platform_scene"] = (
            SceneToolAdapter(self.backend.client, settings) if native else webapi
        )
        self.adapters["platform_doctors"] = (
            DoctorToolAdapter(self.backend.client, settings) if native else webapi
        )

    @classmethod
    def wrap(cls, backend, settings: Settings) -> "MedicalToolGateway":
        """把已有后端（客户端或合成后端）包成网关；已是网关时原样返回。"""
        return backend if isinstance(backend, cls) else cls(settings, backend=backend)

    @property
    def client(self):
        # 问诊流程的其他中台接口继续复用原 HTTP 连接池。
        return self.backend.client

    @client.setter
    def client(self, value) -> None:
        self.backend.client = value

    async def close(self) -> None:
        if self._owns_backend:
            await self.backend.close()

    async def execute(
        self, name: str, arguments: dict, context: ToolContext | None = None
    ) -> ToolResult:
        from meta_agent.orchestration.capabilities import require

        spec = require_enabled(name, self.settings)
        if context is None:
            raise ToolError("scope_required", "业务调用缺少可信运行上下文。", outcome="unsupported")
        operation = "scene.resolve" if name == "navigate_scene" else name
        if (
            name in {"search_doctors", "navigate_scene"}
            and arguments.get("spaceId") != context.scope.space_id
        ):
            raise ToolError(
                "space_scope_mismatch", "请求的空间不属于本轮上下文。", outcome="unsupported"
            )
        if name in context.excluded_tools:
            raise ToolError("client_denied", "该接入方不允许调用此工具。", outcome="unsupported")
        require(
            context.scope,
            self.settings,
            operation,
            excluded=context.excluded_tools,
            arguments=arguments,
        )
        if context.scope.role == "operator" and name == "query_hospital_operations":
            try:
                query = HospitalRequest.model_validate(arguments)
            except ValidationError:
                raise ToolError("invalid_request", "机构指标查询参数无效。") from None
            if query.hospital_name:
                raise ToolError("resource_denied", "机构名称须先在授权目录中核验。")
            arguments = query.model_dump(exclude_unset=True, exclude_none=True)
            arguments["hospital_id"] = context.scope.resource_grant.hospital_id
        # 原有 dry_run 仍统一由合成后端处理，不发起业务请求。
        provider = "ai_webapi" if self.settings.dry_run else spec.provider
        result = await self.adapters[provider].execute(ToolRequest(name, arguments), context)
        if context.scope.resource_grant:
            require(
                context.scope,
                self.settings,
                operation,
                excluded=context.excluded_tools,
                arguments=arguments,
            )
        return result

    async def post(self, name: str, arguments: dict, context: ToolContext | None = None) -> dict:
        return (await self.execute(name, arguments, context)).domain_payload()

    async def resolve_patient_identity(
        self, scope: TrustedScope, *, excluded=frozenset()
    ) -> TrustedScope:
        result = await self.execute(
            "resolve_patient_identity", {}, ToolContext(scope, excluded_tools=excluded)
        )
        return scope.bind(result.domain_payload())

    async def artifact_available(self, url: str) -> bool:
        return await self.backend.artifact_available(url)

    async def directory(self, context: ToolContext) -> list[dict]:
        result = await self.execute("query_hospital_directory", {}, context)
        return result.domain_payload()["directory"]
