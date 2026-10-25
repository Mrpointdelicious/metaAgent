"""
创建日期：2026-10-21
文件功能：验证知识服务调用边界、来源保留、哈希校验与显式失败。
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from langgraph.store.memory import InMemoryStore

from meta_agent.config import Settings
from meta_agent.contracts import DomainError, TaskSpec
from meta_agent.domains.knowledge import KnowledgeAdapter
from meta_agent.infrastructure.repository import Repository


def test_portfolio_history_requires_current_release_retrieval_before_reuse():
    from langchain_core.messages import ToolMessage

    from meta_agent.agent.context import fresh_history

    old = ToolMessage(
        content=json.dumps(
            {
                "tool_name": "search_knowledge",
                "status": "succeeded",
                "facts": [{"value": "old release evidence"}],
                "data": {"retrieval_trace_id": str(uuid4())},
            }
        ),
        tool_call_id="previous-search",
    )
    ctx = SimpleNamespace(settings=SimpleNamespace(agent_history_tokens=10000))
    refreshed = asyncio.run(fresh_history([old], ctx))
    assert json.loads(refreshed[0].content)["status"] == "expired"
    assert "old release evidence" not in refreshed[0].content


def test_model_only_sees_the_knowledge_domain_that_is_actually_connected():
    from meta_agent.agent.tools import available_tools, build_tools

    ctx = SimpleNamespace(
        settings=Settings(_env_file=None, portfolio_enabled=True),
        frontend_context={},
        query="检索文献",
    )
    original = next(tool for tool in build_tools() if tool.name == "search_knowledge")
    selected = available_tools(ctx, [original])[0]
    schema = selected.args_schema.model_json_schema()
    assert schema["properties"]["domain"]["const"] == "health"
    assert schema["properties"]["query"]["maxLength"] == 2000
    assert original.args_schema.model_json_schema()["properties"]["domain"]["enum"] == [
        "health",
        "product",
        "help",
    ]


@pytest.mark.parametrize("mode", ["ok", "empty", "unavailable", "corrupt", "degraded"])
def test_portfolio_retrieval_preserves_sources_and_has_no_fake_fallback(monkeypatch, mode):
    async def scenario():
        settings = Settings(
            _env_file=None,
            portfolio_enabled=True,
            maxkb_enabled=True,
            portfolio_token="private-reader",
            portfolio_kb_ids=[str(uuid4())],
        )
        text = "Guideline evidence about balance assessment."
        chunk = {
            key: str(uuid4()) for key in ("document_id", "version_id", "chunk_id", "release_id")
        }
        chunk.update(
            title="Balance guideline",
            section="Assessment",
            text=text,
            source_locator="guideline.pdf",
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            page=2,
            anchors=[{"page": 2}],
            retrieved_at=datetime.now(UTC).isoformat(),
        )
        trace_id = str(uuid4())
        observed = []

        def handle(request):
            assert request.headers["Authorization"] == "Bearer private-reader"
            body = json.loads(request.content)
            observed.append(body)
            assert body["kb_ids"] == settings.portfolio_kb_ids
            assert "patient_id" not in body and "tenant_id" not in body
            assert body["answer_mode"] == "evidence" and body["strategy"] == "hybrid"
            if mode == "unavailable":
                return httpx.Response(503, json={"sensitive": "provider-data"})
            if mode == "corrupt":
                chunk["text"] = "Changed text"
            return httpx.Response(
                200,
                json={
                    "evidence": [] if mode == "empty" else [chunk],
                    "trace_id": trace_id,
                    "used_context_tokens": 12,
                    "notes": ["reranker_unavailable_using_fusion_order"]
                    if mode == "degraded"
                    else [],
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        monkeypatch.setattr(
            "meta_agent.domains.portfolio.httpx.AsyncClient", lambda **kwargs: client
        )
        repository = Repository(InMemoryStore(), settings)
        ctx = SimpleNamespace(
            settings=settings,
            repository=repository,
            scope=SimpleNamespace(scope_hash="trusted-scope"),
            record=SimpleNamespace(request_id="request", metrics={}),
        )
        task = TaskSpec(
            task_id="knowledge",
            goal_ids=["general"],
            capability="knowledge.search",
            idempotency_key="knowledge",
        )
        args = {"query": "How is balance assessed?", "domain": "health", "kb_ids": ["untrusted"]}
        if mode in {"unavailable", "corrupt"}:
            with pytest.raises(DomainError) as exc:
                await KnowledgeAdapter().execute(task, args, ctx)
            assert exc.value.code == "knowledge_unavailable"
            assert "provider-data" not in exc.value.message
            return
        result = await KnowledgeAdapter().execute(task, args, ctx)
        assert len(observed) == 1 and result.outputs["retrieval_trace_id"] == trace_id
        if mode == "empty":
            assert result.status == "unavailable" and not result.facts
            return
        assert result.status == ("partial" if mode == "degraded" else "succeeded")
        assert result.facts[0].fact.value == text
        assert "PDF 第 2 页" in result.facts[0].label
        saved = await repository.evidence("trusted-scope", result.evidence_ids[0])
        assert saved.payload["retrieval"]["evidence"][0]["content_hash"] == chunk["content_hash"]
        assert await repository.verify_fact("trusted-scope", result.facts[0])
        assert result.outputs["citations"][0]["chunk_id"] == chunk["chunk_id"]

    asyncio.run(scenario())
