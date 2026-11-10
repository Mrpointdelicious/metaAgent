"""
创建日期：2026-11-10
文件功能：验证 MaxKB 原生工作流的协议适配，不在客户端增加召回规则。
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from meta_agent.contracts import DomainError
from meta_agent.domains.knowledge import KnowledgeAdapter
from meta_agent.integrations.knowledge import KnowledgeSourceConfig
from tests.test_remote_knowledge import SOURCES, context, mock_http, response


def native_context():
    ctx = context()
    ctx.settings.knowledge_sources = [
        KnowledgeSourceConfig(
            **{
                **SOURCES[2],
                "transport": "maxkb-workflow",
                "url": "https://maxkb.test/chat/api/app-id/chat/completions",
            }
        )
    ]
    return ctx


def envelope(result):
    return {
        "id": "chat-record-id",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(result, ensure_ascii=False),
                },
            }
        ],
    }


@pytest.mark.parametrize("status", ["ok", "partial", "no_evidence"])
def test_native_workflow_is_one_stateless_call_preserving_contract(monkeypatch, status):
    async def run():
        ctx, seen = native_context(), []
        value = response(status)
        if status == "no_evidence":
            value.update(context="", sources=[], gaps=["没有二楼证据"])

        def handle(req):
            seen.append(req)
            return httpx.Response(200, json=envelope(value))

        mock_http(monkeypatch, handle)
        result = await KnowledgeAdapter().execute(
            SimpleNamespace(task_id="search"), {"query": "介绍二楼", "domain": "product"}, ctx
        )
        assert len(seen) == 1 and seen[0].method == "POST"
        assert seen[0].url.path == "/chat/api/app-id/chat/completions"
        assert seen[0].headers["Authorization"] == "Bearer hospital-token"
        assert json.loads(seen[0].content) == {
            "messages": [{"role": "user", "content": "介绍二楼"}],
            "stream": False,
            "form_data": {
                "domain": "product",
                "space_id": "hospital-a",
                "max_context_tokens": 4000,
                "request_id": ctx.record.request_id,
            },
        }
        assert (
            result.status
            == {"ok": "succeeded", "partial": "partial", "no_evidence": "unavailable"}[status]
        )
        assert result.outputs["retrieval_trace_id"] == value["trace_id"]
        if value["sources"]:
            assert result.outputs["doc_refs"] == [s["ref"] for s in value["sources"]]
            assert result.facts[0].fact.value == value["context"]
            assert await ctx.repository.verify_facts(ctx.scope.scope_hash, result.facts)

    asyncio.run(run())


@pytest.mark.parametrize(
    "malformed",
    [
        {"code": 500, "message": "private upstream detail"},
        {"choices": []},
        {
            "choices": [
                {"finish_reason": "length", "message": {"role": "assistant", "content": "{}"}}
            ]
        },
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "三楼是诊疗中心"},
                }
            ]
        },
        envelope({"answer": "A normal chat answer is not a knowledge contract"}),
    ],
)
def test_chat_output_or_failed_workflow_cannot_become_evidence(monkeypatch, malformed):
    async def run():
        seen = []

        def handle(req):
            seen.append(req)
            return httpx.Response(200, json=malformed)

        mock_http(monkeypatch, handle)
        ctx = native_context()
        with pytest.raises(DomainError) as error:
            await KnowledgeAdapter().execute(
                SimpleNamespace(task_id="k"), {"query": "介绍二楼"}, ctx
            )
        assert error.value.code == "knowledge_unavailable"
        assert "private" not in str(error.value)
        assert len(seen) == 1 and not ctx.record.metrics.get("knowledge_retrieval")

    asyncio.run(run())


@pytest.mark.parametrize(
    "code,retryable,expected,outcome",
    [
        ("knowledge_access_denied", False, "knowledge_access_denied", "unsupported"),
        ("knowledge_access_denied", True, "knowledge_access_denied", "unsupported"),
        ("upstream_unavailable", True, "knowledge_unavailable", "unavailable"),
    ],
)
def test_native_guard_errors_are_not_misreported_as_empty_evidence(
    monkeypatch, code, retryable, expected, outcome
):
    async def run():
        mock_http(
            monkeypatch,
            lambda req: httpx.Response(
                200,
                json=envelope(
                    {
                        "error": {"code": code, "retryable": retryable},
                        "request_id": "r",
                    }
                ),
            ),
        )
        with pytest.raises(DomainError) as error:
            await KnowledgeAdapter().execute(
                SimpleNamespace(task_id="k"), {"query": "介绍二楼"}, native_context()
            )
        assert error.value.code == expected and error.value.outcome == outcome
        assert error.value.retryable == (retryable and expected != "knowledge_access_denied")

    asyncio.run(run())


def test_native_transport_rejects_a_management_api_url():
    with pytest.raises(ValidationError, match="chat/completions"):
        KnowledgeSourceConfig(
            **{**SOURCES[2], "transport": "maxkb-workflow", "url": "http://maxkb.test/admin/api"}
        )
