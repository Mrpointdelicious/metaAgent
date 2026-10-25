"""
创建日期：2026-10-25
文件功能：验证多知识源的契约、可信身份、调用预算、失败隔离和来源传递。
"""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage
from langgraph.store.memory import InMemoryStore
from pydantic import ValidationError

from meta_agent.agent.tools import available_tools, build_tools
from meta_agent.application.context import RunContext
from meta_agent.config import Settings
from meta_agent.contracts import DomainError, Goal
from meta_agent.domains.knowledge import KnowledgeAdapter
from meta_agent.infrastructure.repository import Repository
from meta_agent.integrations.knowledge import contract
from tests.helpers.runtime import SeedPlanner, answers, request, runtime
from tests.test_single_agent import activate, tool_call

SOURCES = [
    {
        "id": "medical",
        "description": "医学文献",
        "domains": ["health"],
        "url": "https://medical.test/search",
        "token": "medical-token",
    },
    {
        "id": "alternative",
        "description": "另一文献库",
        "domains": ["health"],
        "url": "https://other.test/find",
        "token_env": "OTHER_KNOWLEDGE_TOKEN",
    },
    {
        "id": "hospital",
        "description": "医院说明",
        "domains": ["help", "product"],
        "scoped_domains": ["help", "product"],
        "url": "https://hospital.test/search",
        "token": "hospital-token",
    },
]


def response(status="ok"):
    return {
        "status": status,
        "context": "[S1] Synthetic published evidence.",
        "sources": [{"id": "S1", "title": "说明", "locator": "第 12 页", "ref": "versioned-ref"}],
        "gaps": ["另一子问题缺少证据"] if status == "partial" else [],
        "trace_id": "opaque-trace",
    }


def context(**overrides):
    ctx = SimpleNamespace(
        settings=Settings(_env_file=None, knowledge_sources=SOURCES, **overrides),
        scope=SimpleNamespace(space_id="hospital-a", scope_hash="scope"),
        record=SimpleNamespace(request_id=str(uuid4()), metrics={}),
        repository=Repository(InMemoryStore(), Settings(_env_file=None)),
        emitter=SimpleNamespace(persistence_failed=False),
        runtime_guard=None,
        tool_calls=0,
        tool_limiter=asyncio.Semaphore(2),
    )
    ctx.tool_slot = lambda: RunContext.tool_slot(ctx)
    return ctx


def mock_http(monkeypatch, handle):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        "meta_agent.integrations.knowledge.httpx.AsyncClient",
        lambda **kw: client_type(**{**kw, "transport": httpx.MockTransport(handle)}),
    )


def test_tool_exposes_configured_sources_without_connection_or_identity_details():
    ctx = SimpleNamespace(
        settings=Settings(_env_file=None, knowledge_sources=SOURCES),
        query="查资料",
        frontend_context={},
    )
    original = next(tool for tool in build_tools() if tool.name == "search_knowledge")
    tool = available_tools(ctx, [original])[0]
    schema = tool.args_schema
    assert schema["properties"]["source"]["enum"] == ["medical", "alternative", "hospital"]
    assert schema["properties"]["query"]["maxLength"] == 2000
    assert (
        not {"scope", "space_id", "url", "token", "kb_ids", "limits"} & schema["properties"].keys()
    )
    assert "另一文献库" in tool.description and "medical-token" not in str(schema)


def test_selects_independent_provider_and_preserves_verifiable_evidence(monkeypatch):
    async def check():
        seen = []

        def handle(req):
            seen.append(req)
            return httpx.Response(200, json=response())

        mock_http(monkeypatch, handle)
        monkeypatch.setenv("OTHER_KNOWLEDGE_TOKEN", "alternative-secret")
        ctx = context()
        adapter = KnowledgeAdapter()
        task = SimpleNamespace(task_id="search")
        await adapter.execute(task, {"query": "问题", "domain": "health"}, ctx)
        result = await adapter.execute(
            task,
            {
                "query": "问题",
                "domain": "health",
                "source": "alternative",
            },
            ctx,
        )
        await adapter.execute(task, {"query": "操作问题", "domain": "help"}, ctx)
        assert [req.url.host for req in seen] == ["medical.test", "other.test", "hospital.test"]
        assert seen[1].headers["Authorization"] == "Bearer alternative-secret"
        assert json.loads(seen[0].content)["scope"] == {}
        assert json.loads(seen[2].content)["scope"] == {"space_id": "hospital-a"}
        assert (
            not {"kb_ids", "strategy", "rerank", "patient_id"} & json.loads(seen[2].content).keys()
        )
        assert result.outputs["source_id"] == "alternative"
        assert result.outputs["citations"][0]["locator"] == "第 12 页"
        assert await ctx.repository.verify_facts(ctx.scope.scope_hash, result.facts)
        assert ctx.tool_calls == 3

    asyncio.run(check())


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("partial", "partial"),
        ("empty", "unavailable"),
        ("503", "knowledge_unavailable"),
        ("403", "knowledge_access_denied"),
        ("malformed", "knowledge_unavailable"),
        ("inconsistent", "knowledge_unavailable"),
        ("duplicate", "knowledge_unavailable"),
        ("oversized", "knowledge_unavailable"),
        ("unknown_status", "knowledge_unavailable"),
        ("timeout", "knowledge_unavailable"),
    ],
)
def test_provider_failure_does_not_switch_source_or_use_local_corpus(monkeypatch, mode, expected):
    async def check():
        seen = []

        def handle(req):
            seen.append(req.url.host)
            value = response("partial" if mode == "partial" else "ok")
            if mode in {"503", "403"}:
                return httpx.Response(int(mode), json={"error": {"code": "failure"}})
            if mode == "timeout":
                raise httpx.ReadTimeout("provider unavailable", request=req)
            if mode == "malformed":
                return httpx.Response(200, content=b"not-json")
            if mode in {"empty", "inconsistent"}:
                value.update(status="no_evidence", context="", sources=[])
            if mode == "inconsistent":
                value["context"] = "unexpected text"
            if mode == "duplicate":
                value["sources"] *= 2
            if mode == "unknown_status":
                value["status"] = "new-meaning"
            if mode == "oversized":
                value["context"] = "x" * 5000
            return httpx.Response(200, json=value)

        mock_http(monkeypatch, handle)
        ctx = context(max_tool_response_bytes=4096, knowledge_corpus_path="must-not-be-read.json")
        if mode in {"partial", "empty"}:
            result = await KnowledgeAdapter().execute(
                SimpleNamespace(task_id="search"), {"query": "问题"}, ctx
            )
            assert result.status == expected
            if mode == "partial":
                assert result.outputs["gaps"] and result.facts
        else:
            with pytest.raises(DomainError) as rejected:
                await KnowledgeAdapter().execute(
                    SimpleNamespace(task_id="search"), {"query": "问题"}, ctx
                )
            assert rejected.value.code == expected
            assert rejected.value.retryable == (mode in {"503", "timeout"})
        assert seen == ["medical.test"]

    asyncio.run(check())


@pytest.mark.parametrize(
    "args",
    [
        {"query": "q", "scope": {"space_id": "other"}},
        {"query": "q", "source": "not-configured"},
        {"query": "q", "domain": "help", "source": "medical"},
    ],
)
def test_model_cannot_override_scope_or_use_an_unconfigured_source(monkeypatch, args):
    async def check():
        def no_http(req):
            pytest.fail("Invalid tool input must not call any provider")

        mock_http(monkeypatch, no_http)
        with pytest.raises(DomainError):
            await KnowledgeAdapter().execute(SimpleNamespace(task_id="search"), args, context())

    asyncio.run(check())


def test_real_agent_loop_uses_dynamic_schema_and_shared_call_budget(monkeypatch):
    async def check():
        seen = []

        def handle(req):
            seen.append(req.url.host)
            return httpx.Response(200, json=response())

        mock_http(monkeypatch, handle)
        monkeypatch.setenv("OTHER_KNOWLEDGE_TOKEN", "other-token")
        async with runtime(knowledge_sources=SOURCES, max_tool_calls=1) as (container, _):
            activate(
                container,
                [
                    tool_call("search_knowledge", {"query": "问题", "source": "alternative"}),
                    tool_call("search_knowledge", {"query": "补查"}, call_id="second"),
                    AIMessage("依据已取得的说明资料回答。"),
                ],
            )
            run = await container.application.execute(request("查找知识资料"))
            assert "说明资料" in answers(run.record)
            assert seen == ["other.test"]
            assert run.record.metrics["knowledge_retrieval"][0]["source_id"] == "alternative"
            assert any(result.code == "tool_budget" for result in run.record.results.values())

    asyncio.run(check())


def test_source_configuration_rejects_ambiguous_ids_and_embedded_credentials():
    with pytest.raises(ValidationError, match="unique"):
        Settings(_env_file=None, knowledge_sources=[SOURCES[0], SOURCES[0]])
    with pytest.raises(ValidationError, match="credentials"):
        Settings(_env_file=None, knowledge_sources=[{**SOURCES[0], "url": "https://u:p@host/api"}])
    assert contract()["info"]["version"] == "1.0.0"


def test_legacy_planner_can_use_a_configured_knowledge_source(monkeypatch):
    async def check():
        mock_http(monkeypatch, lambda req: httpx.Response(200, json=response()))
        async with runtime(knowledge_sources=SOURCES) as (container, _):
            container.application.planner = SeedPlanner(
                [
                    Goal(
                        goal_id="knowledge",
                        kind="knowledge_query",
                        domain="health",
                        query_span="问题",
                    )
                ]
            )
            run = await container.application.execute(request("问题"))
            assert run.record.status == "succeeded"
            assert "Synthetic published evidence" in answers(run.record)
            assert "versioned-ref" in answers(run.record)

    asyncio.run(check())


def test_additive_provider_metadata_is_accepted_but_not_passed_to_model(monkeypatch):
    async def check():
        value = response()
        value["internal_policy"] = "not for model"
        value["sources"][0]["internal_metadata"] = "not for model"
        mock_http(monkeypatch, lambda req: httpx.Response(200, json=value))
        result = await KnowledgeAdapter().execute(
            SimpleNamespace(task_id="search"), {"query": "q"}, context()
        )
        assert "not for model" not in result.model_dump_json()

    asyncio.run(check())


def test_dify_displays_distinct_common_contract_sources():
    spec = importlib.util.spec_from_file_location(
        "knowledge_present", Path(__file__).parents[1] / "workflows/dify/present.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    body = {
        "status": "succeeded",
        "metrics": {
            "knowledge_retrieval": [
                {
                    "source_id": "medical",
                    "sources": [
                        {"title": "来源甲", "locator": "第 12 页", "ref": "a"},
                        {"title": "来源乙", "locator": "第 8 页", "ref": "b"},
                    ],
                },
            ]
        },
    }
    text = module.main(json.dumps(body), 200)["display_text"]
    assert "来源甲（第 12 页）" in text and "来源乙（第 8 页）" in text
