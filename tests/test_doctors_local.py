"""
创建日期：2026-11-13
文件功能：验证在线医生本地查询（直连 webyun 在线医生列表、不经过 AI_WebApi）的契约与降级行为。

上游真实响应形状（2026-11-13 实测 http://192.168.8.71:4000/api/users/all）：

    无医生：{"code":200,"message":"暂无在线医生","data":[]}
    未登录：{"code":401,"message":"无权限，请登录"}      # HTTP 仍是 200

因此本地实现必须：① 结构与 .NET DoctorToolsService 的 DTO 一致；
② 把 body.code != 200 判为鉴权失败，而不是「当前没有在线医生」。
"""

import asyncio

import httpx
import pytest

from meta_agent.agent.tools import invoke_tool
from meta_agent.domains import doctors_local
from meta_agent.domains.doctors_local import (
    DoctorUpstreamAuthError,
    build_envelope,
    resolve_local_doctors,
)
from meta_agent.tools.ai_webapi import AIWebApiClient, BackendCallError
from tests.helpers.integration import context
from tests.helpers.runtime import runtime
from tests.helpers.settings import AppTestSettings

LOGIN_PATH = "/webyun/api/Login/Login"
DOCTORS_PATH = "/api/users/all"


@pytest.fixture(autouse=True)
def _clear_token_cache():
    doctors_local.reset_token_cache()
    yield
    doctors_local.reset_token_cache()


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


def _transport(doctor_body, *, login_body=None, doctor_status=200, seen=None):
    """模拟登录 + 在线医生列表，记录全部请求。"""
    seen = seen if seen is not None else []
    login = login_body if login_body is not None else {"code": 200, "data": {"token": "token-1"}}

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == LOGIN_PATH:
            return httpx.Response(200, json=login)
        if request.url.path == DOCTORS_PATH:
            return httpx.Response(doctor_status, json=doctor_body)
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler), seen


# --------------------------------------------------------------------------- #
# 封套字段：与 .NET DTO 逐字段一致
# --------------------------------------------------------------------------- #


def test_single_doctor_produces_recommend_command():
    body = build_envelope(["张三"], ["5848"], ["13900000001"])
    assert body["status"] == 200
    data = body["data"]
    assert data["result"] == "一个医生"
    assert data["doctorNames"] == "张三"
    assert data["doctorIds"] == "5848"
    assert data["doctorPhones"] == "13900000001"
    assert data["command"] == "[DoctorRecommend:]13900000001"
    assert data["count"] == 1


def test_multiple_doctors_have_no_auto_dial_command():
    body = build_envelope(["张三", "李四"], ["5848", "5849"], ["139", "138"])
    data = body["data"]
    assert data["result"] == "多个医生"
    assert data["doctorNames"] == "张三、李四"
    assert data["doctorIds"] == "5848,5849"
    assert data["command"] == ""
    assert data["count"] == 2


def test_empty_list_keeps_dotnet_defaults():
    data = build_envelope([], [], [])["data"]
    assert data == {
        "result": "无医生",
        "doctorNames": "",
        "doctorIds": "0",
        "doctorPhones": "",
        "command": "",
        "count": 0,
    }


# --------------------------------------------------------------------------- #
# 客户端接线：默认走本地，不打 .NET 端点
# --------------------------------------------------------------------------- #


def test_default_path_never_calls_webapi():
    async def check():
        seen = []
        transport, _ = _transport(
            {
                "code": 200,
                "message": "获取成功",
                "data": [{"id": 5848, "name": "张三", "phone": "13900000001"}],
            },
            seen=seen,
        )
        client = AIWebApiClient(_settings(), transport=transport)
        try:
            body = await client.post("search_doctors", {"spaceId": "space-1"})
        finally:
            await client.close()
        assert body["status"] == 200
        assert body["data"]["count"] == 1
        assert body["data"]["command"] == "[DoctorRecommend:]13900000001"
        assert any(url.endswith(LOGIN_PATH) for url in seen), seen
        assert any(DOCTORS_PATH in url for url in seen), seen
        assert not any("/api/ai/rehab/tools" in url for url in seen), seen
        assert any("spaceId=space-1" in url for url in seen), seen

    asyncio.run(check())


def test_empty_message_maps_to_no_doctor():
    async def check():
        transport, _ = _transport({"code": 200, "message": "暂无在线医生", "data": []})
        client = AIWebApiClient(_settings(), transport=transport)
        try:
            body = await client.post("search_doctors", {"spaceId": "space-1"})
        finally:
            await client.close()
        assert body["data"]["count"] == 0
        assert body["data"]["result"] == "无医生"

    asyncio.run(check())


def test_token_is_cached_across_queries():
    async def check():
        seen = []
        transport, _ = _transport({"code": 200, "message": "暂无在线医生", "data": []}, seen=seen)
        client = AIWebApiClient(_settings(), transport=transport)
        try:
            await client.post("search_doctors", {"spaceId": "space-1"})
            await client.post("search_doctors", {"spaceId": "space-1"})
        finally:
            await client.close()
        assert sum(1 for url in seen if url.endswith(LOGIN_PATH)) == 1, seen

    asyncio.run(check())


def test_upstream_auth_failure_is_not_reported_as_no_doctor():
    """上游 401 走 HTTP 200，绝不能降级成「当前没有在线医生」。"""

    async def check():
        transport, _ = _transport({"code": 401, "message": "无权限，请登录"})
        client = AIWebApiClient(_settings(), transport=transport)
        try:
            with pytest.raises(BackendCallError) as caught:
                await client.post("search_doctors", {"spaceId": "space-1"})
        finally:
            await client.close()
        assert caught.value.code == "doctor_upstream_unauthorized"
        assert "在线医生" in caught.value.message

    asyncio.run(check())


def test_login_without_token_raises_auth_error():
    async def check():
        transport, _ = _transport(
            {"code": 200, "data": []}, login_body={"code": 200, "message": "获取成功", "data": {}}
        )
        client = AIWebApiClient(_settings(), transport=transport)
        try:
            with pytest.raises(BackendCallError) as caught:
                await client.post("search_doctors", {"spaceId": "space-1"})
        finally:
            await client.close()
        assert caught.value.code == "doctor_upstream_unauthorized"

    asyncio.run(check())


def test_non_auth_business_error_is_invalid_contract():
    """上游 code=500（HTTP 200）不是鉴权问题，按无效数据处理而不是「无医生」。"""

    async def check():
        transport, _ = _transport({"code": 500, "message": "系统异常"})
        client = AIWebApiClient(_settings(), transport=transport)
        try:
            with pytest.raises(BackendCallError) as caught:
                await client.post("search_doctors", {"spaceId": "space-1"})
        finally:
            await client.close()
        assert caught.value.code == "invalid_contract"

    asyncio.run(check())


def test_upstream_http_error_is_retryable():
    async def check():
        transport, _ = _transport({"message": "boom"}, doctor_status=503)
        client = AIWebApiClient(_settings(), transport=transport)
        try:
            with pytest.raises(BackendCallError) as caught:
                await client.post("search_doctors", {"spaceId": "space-1"})
        finally:
            await client.close()
        assert caught.value.code == "http_503"
        assert caught.value.retryable is True

    asyncio.run(check())


def test_rollback_flag_restores_webapi_call():
    async def check():
        seen = []
        canned = build_envelope(["张三"], ["5848"], ["139"])

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json=canned)

        client = AIWebApiClient(
            _settings(doctors_local_enabled=False), transport=httpx.MockTransport(handler)
        )
        try:
            body = await client.post("search_doctors", {"spaceId": "space-1"})
        finally:
            await client.close()
        assert body == canned
        assert any(url.endswith("/api/ai/rehab/tools/search_doctors") for url in seen), seen
        assert not any(url.endswith(LOGIN_PATH) for url in seen), seen

    asyncio.run(check())


def test_dry_run_keeps_demo_response():
    async def check():
        client = AIWebApiClient(AppTestSettings(dry_run=True))
        try:
            body = await client.post("search_doctors", {"spaceId": "space-1"})
        finally:
            await client.close()
        assert body["data"]["count"] == 0

    asyncio.run(check())


# --------------------------------------------------------------------------- #
# 与既有 evidence / facts 链兼容
# --------------------------------------------------------------------------- #


def test_doctor_adapter_consumes_local_envelope():
    """本地封套能过 numeric_envelope 与 FactBuilder，产出 doctor_count / doctor_names。"""

    async def check():
        transport, _ = _transport(
            {
                "code": 200,
                "message": "获取成功",
                "data": [
                    {"id": 5848, "name": "张三", "phone": "13900000001"},
                    {"id": 5849, "name": "李四", "phone": "13900000002"},
                ],
            }
        )
        async with runtime() as (container, backend):
            client = AIWebApiClient(_settings(), transport=transport)
            ctx = context(container, "有哪些医生在线")
            ctx.backend = client
            try:
                result = await invoke_tool(ctx, "search_doctors", {})
            finally:
                await client.close()
            assert result["status"] == "succeeded", result
            facts = {fact["label"]: fact["value"] for fact in result["facts"]}
            assert facts["当前在线医生数"] == 2
            assert facts["在线医生"] == "张三、李四"
            assert result["data"]["has_results"] is True

    asyncio.run(check())


def test_resolve_local_doctors_uses_scene_default_space():
    """payload 无 spaceId 时回退到配置的默认空间。"""

    async def check():
        seen = []
        transport, _ = _transport({"code": 200, "message": "暂无在线医生", "data": []}, seen=seen)
        settings = _settings(doctor_default_space_id="space-default")
        async with httpx.AsyncClient(transport=transport) as client:
            body = await resolve_local_doctors(client, settings, {})
        assert body["data"]["count"] == 0
        assert any("spaceId=space-default" in url for url in seen), seen

    asyncio.run(check())


def test_missing_credentials_raise_auth_error():
    async def check():
        transport, seen = _transport({"code": 200, "message": "暂无在线医生", "data": []})
        settings = _settings(webyun_login_phone="", webyun_login_password="")
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(DoctorUpstreamAuthError):
                await resolve_local_doctors(client, settings, {"spaceId": "space-1"})
        assert seen == []

    asyncio.run(check())
