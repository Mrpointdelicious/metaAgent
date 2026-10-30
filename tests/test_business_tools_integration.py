"""
创建日期：2026-10-30
文件功能：验证医院目录、医生只读选择、公司检索和会话工具安全边界。
"""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from meta_agent.agent.tools import invoke_tool
from meta_agent.contracts import DomainError
from meta_agent.memory.redaction import retained_messages
from meta_agent.tools.ai_webapi import AIWebApiClient
from tests.helpers.integration import context
from tests.helpers.runtime import runtime
from tests.helpers.settings import AppTestSettings
from tests.test_remote_knowledge import mock_http, response


def test_hospital_directory_filters_and_rejects_invented_ids():
    async def check():
        async with runtime() as (container, backend):
            ctx = context(container)
            denied = await invoke_tool(ctx, "query_hospital_directory", {})
            assert denied["code"] == "tool_not_allowed"
            ctx.scope = replace(ctx.scope, role="operator")
            result = await invoke_tool(ctx, "query_hospital_directory", {"name": "演示"})
            assert result["data"]["directory"][0]["hospital_id"] == 1
            assert await ctx.repository.evidence(ctx.scope.scope_hash, result["evidence_id"])
            empty = await invoke_tool(ctx, "query_hospital_directory", {"name": "不存在"})
            assert empty["data"]["total"] == 0
            forged = await invoke_tool(ctx, "query_hospital_operations", {"hospital_id": 999})
            assert forged["code"] == "hospital_required"
            assert not backend.calls

    asyncio.run(check())


@pytest.mark.parametrize(
    "body",
    [
        [{"hospital_id": 4, "hospital_name": "测试机构"}],
        [{"hospital_id": True, "hospital_name": "bad"}],
        [{"hospital_id": 4, "hospital_name": "a"}, {"hospital_id": 4, "hospital_name": "b"}],
    ],
)
def test_directory_uses_existing_fixed_get_with_authentication_and_validates_rows(body):
    async def check():
        def handle(request):
            assert request.method == "GET"
            assert request.url.path == "/api/ai/hospital-operations/directory"
            assert request.headers["authorization"] == "Bearer synthetic-backend"
            return httpx.Response(200, json=body)

        settings = AppTestSettings(
            dry_run=False,
            ai_webapi_bearer_token="synthetic-backend",
            ai_webapi_base_url="https://backend.test/api/ai/patients/tools",
        )
        client = AIWebApiClient(settings, transport=httpx.MockTransport(handle))
        try:
            if body[0]["hospital_name"] == "测试机构":
                assert await client.directory() == body
            else:
                with pytest.raises(DomainError, match="机构目录格式无效"):
                    await client.directory()
        finally:
            await client.close()

    asyncio.run(check())


def test_doctor_search_selection_and_registration_are_separate():
    async def check():
        async with runtime() as (container, backend):
            backend.overrides["search_doctors"] = {
                "status": 200,
                "data": {
                    "count": 2,
                    "doctorNames": "甲医生、乙医生",
                    "doctorPhones": "13800000000",
                    "command": "untrusted",
                },
            }
            ctx = context(container, "选择第二个")
            missing = await invoke_tool(ctx, "resolve_doctor_selection", {"selection": "第二个"})
            assert missing["code"] == "doctor_list_required"
            doctors = await invoke_tool(ctx, "search_online_doctors", {})
            assert "13800000000" not in json.dumps(doctors) and "untrusted" not in json.dumps(
                doctors
            )
            selected = await invoke_tool(ctx, "resolve_doctor_selection", {"selection": "第二个"})
            assert selected["data"]["doctor_name"] == "乙医生"
            registration = await invoke_tool(ctx, "request_registration", {})
            assert registration["code"] == "registration_authorization_unavailable"
            assert len(backend.calls) == 1 and not ctx.record.action_delivery

    asyncio.run(check())


def test_company_source_reuses_evidence_scoping_budget_and_no_auth_fallback(monkeypatch):
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    mock_http(monkeypatch, handle)

    async def check():
        sources = [
            {
                "id": "company",
                "description": "公司资料",
                "domains": ["help"],
                "scoped_domains": ["help"],
                "url": "https://company.test/search",
                "token": "synthetic-company",
            }
        ]
        async with runtime(knowledge_sources=sources) as (
            container,
            _,
        ):
            ctx = context(container, "介绍公司")
            result = await invoke_tool(
                ctx,
                "search_knowledge",
                {"query": "介绍公司", "domain": "help", "source": "company"},
            )
            assert result["status"] == "succeeded" and ctx.tool_calls == 1
            assert seen[0]["scope"] == {"space_id": "space"}
            assert result["data"]["citations"][0]["locator"] == "第 12 页"
            assert await ctx.repository.evidence(ctx.scope.scope_hash, result["evidence_id"])
            assert await ctx.repository.evidence("other-scope", result["evidence_id"]) is None
            forged = await invoke_tool(
                ctx, "search_knowledge", {"query": "x", "url": "https://bad.test"}
            )
            assert forged["code"] == "invalid_knowledge_request" and len(seen) == 1

    asyncio.run(check())


def test_current_history_tool_has_no_model_selected_identity_or_url():
    async def check():
        async with runtime() as (container, _):
            ctx = context(container)
            ctx.memory.turns = [{"user": "之前的问题", "assistant": "之前的答案"}]
            await ctx.repository.save_conversation(ctx.scope.thread_id("c1"), ctx.memory)
            result = await invoke_tool(ctx, "open_chat_history", {})
            assert result["data"]["turns"][0]["assistant"] == "之前的答案"
            bad = await invoke_tool(ctx, "open_chat_history", {"user": "other"})
            assert bad["code"] == "invalid_arguments"
            ctx.scope = replace(ctx.scope, end_user_id="other")
            result = await invoke_tool(ctx, "open_chat_history", {})
            assert result["data"]["turns"] == []

    asyncio.run(check())


def test_retained_messages_remove_credentials_phones_and_authorized_urls():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    messages = [
        HumanMessage(content="手机号13800000000 password=synthetic-secret"),
        AIMessage(content="https://report.test/image?access_grant=synthetic-secret"),
        AIMessage(
            content="",
            additional_kwargs={
                "tool_calls": [
                    {
                        "id": "t",
                        "type": "function",
                        "function": {
                            "name": "search_knowledge",
                            "arguments": json.dumps({"password": "synthetic-secret"}),
                        },
                    }
                ]
            },
        ),
        ToolMessage(
            tool_call_id="t",
            content=json.dumps(
                {
                    "identity_context_token": "synthetic-secret",
                    "patient_name": "private",
                    "data": {"phone": "13800000000", "session_ref": "opaque-ref"},
                }
            ),
        ),
    ]
    saved = retained_messages(messages)
    assert "synthetic-secret" not in str(saved) and "13800000000" not in str(saved)
    assert "private" not in str(saved) and "opaque-ref" in str(saved)
    assert "13800000000" in messages[0].content
