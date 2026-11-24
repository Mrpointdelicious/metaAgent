"""
创建日期：2026-11-17
文件功能：验证医疗工具网关的注册表单一来源、提供方路由与封套透传。

对齐的现状（合并前）：`RunContext.call` 直接调用 `AIWebApiClient.post`，场景/医生在
客户端内部 if 分发；合并后统一由 `MedicalToolGateway` 按注册表选提供方，行为必须不变：
    - 生产（真实客户端）→ 场景/医生走本地中台适配器，不请求 .NET；
    - dry_run → 一律走 AI_WebApi 适配器（合成数据），不发任何请求；
    - 合成后端（测试注入）→ 回落该后端的 post，便于用 overrides 模拟。
"""

import asyncio

import httpx
import pytest

from meta_agent.medical_tools.errors import ToolError
from meta_agent.medical_tools.gateway import MedicalToolGateway
from meta_agent.medical_tools.registry import registry, require_enabled
from meta_agent.tools import ai_webapi
from meta_agent.tools.ai_webapi import AIWebApiClient
from tests.helpers.settings import AppTestSettings

DOCTOR_BODY = {
    "code": 200,
    "message": "在线医生列表获取成功",
    "data": [{"id": 2686, "name": "医生5", "phone": "18900000005"}],
}


def _settings(**kwargs):
    values = {
        "dry_run": False,
        "ai_webapi_base_url": "http://webapi.test/api/ai/patients/tools",
        "ai_webapi_rehab_base_url": "http://webapi.test/api/ai/rehab/tools",
        "webyun_login_phone": "15932910402",
        "webyun_login_password": "secret",
    }
    values.update(kwargs)
    return AppTestSettings(**values)


def _transport(seen: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/Login/Login"):
            return httpx.Response(200, json={"code": 200, "data": {"token": "token-1"}})
        if request.url.path == "/api/users":
            return httpx.Response(200, json=DOCTOR_BODY)
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler)


class SyntheticBackend:
    """与 tests.helpers.runtime.Backend 同构的最小合成后端。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.artifact_ok = True
        self.directory_entries = [{"hospital_id": 1, "hospital_name": "合成医院"}]

    async def post(self, endpoint: str, payload: dict) -> dict:
        self.calls.append((endpoint, payload))
        return {"status": 200, "message": "查询成功", "data": {"count": 0}}

    async def artifact_available(self, url: str) -> bool:
        return self.artifact_ok

    async def directory(self) -> list[dict]:
        return list(self.directory_entries)


def test_registry_is_single_source_for_endpoint_sets():
    """ai_webapi 的端点集合必须就是注册表里的对象，不允许各写一份。"""
    assert ai_webapi.PATIENT_ENDPOINTS is not None
    settings = _settings()
    specs = registry(settings)
    assert "search_doctors" in specs
    assert specs["search_doctors"].provider == "platform_doctors"
    assert specs["navigate_scene"].provider == "platform_scene"
    # 关掉本地开关就回到 .NET 实现。
    disabled = registry(_settings(doctors_local_enabled=False, scene_local_enabled=False))
    assert disabled["search_doctors"].provider == "ai_webapi"
    assert disabled["navigate_scene"].provider == "ai_webapi"


def test_unknown_tool_and_disabled_capability_keep_original_codes():
    with pytest.raises(ToolError) as unknown:
        require_enabled("not_a_tool", _settings())
    assert unknown.value.code == "unknown_tool"

    with pytest.raises(ToolError) as disabled:
        require_enabled(
            "generate_irecog_single_session_report", _settings(irecog_reports_enabled=False)
        )
    assert disabled.value.code == "capability_disabled"
    assert disabled.value.outcome == "unsupported"


def test_gateway_routes_doctors_to_local_platform_endpoint():
    """真实客户端 + 本地开关：/api/users（在线接诊口径），不去 .NET 的 rehab 端点。"""

    async def check():
        seen: list[str] = []
        client = AIWebApiClient(_settings(), transport=_transport(seen))
        gateway = MedicalToolGateway(_settings(), backend=client)
        try:
            body = await gateway.post("search_doctors", {"spaceId": "67ecd511"})
        finally:
            await client.close()
        paths = [httpx.URL(url).path for url in seen]
        assert "/api/users" in paths, seen
        assert not any("rehab" in path for path in paths), seen
        assert body["data"]["count"] == 1
        assert body["data"]["command"] == "[DoctorRecommend:]18900000005"

    asyncio.run(check())


def test_gateway_falls_back_to_synthetic_backend():
    """注入的合成后端不是 AIWebApiClient：场景/医生回落该后端的 post。"""

    async def check():
        backend = SyntheticBackend()
        gateway = MedicalToolGateway(_settings(), backend=backend)
        await gateway.post("search_doctors", {"spaceId": "space"})
        assert [name for name, _ in backend.calls] == ["search_doctors"]
        assert await gateway.artifact_available("http://x/y.png") is True
        assert await gateway.directory() == backend.directory_entries

    asyncio.run(check())


def test_dry_run_never_touches_upstream():
    async def check():
        seen: list[str] = []
        settings = _settings(dry_run=True)
        client = AIWebApiClient(settings, transport=_transport(seen))
        gateway = MedicalToolGateway(settings, backend=client)
        try:
            await gateway.post("search_doctors", {"spaceId": "space"})
        finally:
            await client.close()
        assert seen == []

    asyncio.run(check())
