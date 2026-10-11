"""
创建日期：2026-10-11
文件功能：以明确标记的MockHTTP测试验证远程知识边界，不计入真实联调。
"""

import asyncio
import hashlib
import hmac
import json
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import pytest
from langchain_core.messages import ToolMessage

from meta_agent.application.context import RunContext
from meta_agent.context.budget import LLMBudget
from meta_agent.contracts import ConversationState, DomainError, RunRecord, TaskSpec
from meta_agent.domains import remote_knowledge as remote
from meta_agent.domains.knowledge import KnowledgeAdapter
from meta_agent.domains.knowledge_citations import audit_remote_answer
from meta_agent.orchestration.identity import TrustedScope
from tests.helpers.runtime import runtime

TOKEN = "test-demo-token-" + "x" * 32
PIN = "a" * 64
SCOPE = TrustedScope("integration-demo", "demo-reviewer", role="operator")
ARGS = {"query": "康复研究方案", "domain": "health"}
TEXT = "合成方案计划研究步行训练；未提供患者测量值。"
REF = "protocol-a@v1#outcomes"


def response(rid):
    source = "repo://demo/protocol-a"
    return {
        "schema_version": "demo-1",
        "request_id": rid,
        "retrieval_run_id": str(uuid4()),
        "backend": "portfolio_demo_bm25",
        "strategy": "bm25-k1-1.2-b-0.75",
        "approval": "technical_demo_only",
        "clinical_approval": False,
        "corpus_version": "demo-rehab-protocols-v1",
        "corpus_sha256": PIN,
        "status": "ok",
        "elapsed_ms": 1.2,
        "budget_truncated": False,
        "evidence": [
            {
                "source_id": str(uuid5(NAMESPACE_URL, source)),
                "chunk_id": str(uuid5(NAMESPACE_URL, source + "@v1#outcomes")),
                "doc_ref": REF,
                "document_version": "v1",
                "title": "合成方案甲",
                "source": source,
                "section": "主要结局",
                "section_id": "outcomes",
                "page": None,
                "start": 0,
                "end": len(TEXT),
                "text": TEXT,
                "text_sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
                "score": 1.2,
            }
        ],
    }


def sign(body):
    body = deepcopy(body)
    body["signature"] = hmac.new(TOKEN.encode(), remote.canonical(body), hashlib.sha256).hexdigest()
    return body


def context(c, scope=SCOPE):
    return RunContext(
        ARGS["query"],
        scope,
        RunRecord(
            run_id="run",
            request_id="request",
            request_hash="hash",
            scope_key=scope.scope_hash,
            conversation_id="conv",
        ),
        ConversationState(),
        c.settings,
        c.repository,
        None,
        None,
        LLMBudget(8),
        asyncio.Semaphore(1),
    )


TASK = TaskSpec(
    task_id="test",
    goal_ids=["agent"],
    capability="knowledge.search",
    arguments=ARGS,
    idempotency_key="test",
)


def install(monkeypatch, mutate=None, status=200, error=None, raw=None):
    calls = []
    original = httpx.AsyncClient

    def handler(request):
        calls.append(request)
        assert "tenant_id" not in json.loads(request.content)
        if error:
            raise error
        if raw is not None:
            return httpx.Response(status, content=raw)
        body = response(request.headers["X-Request-ID"])
        if mutate:
            mutate(body)
        return httpx.Response(status, json=sign(body))

    monkeypatch.setattr(
        remote,
        "HTTPClient",
        lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
    )
    return calls


def config(**kw):
    return {
        "knowledge_backend": "remote_rag",
        "knowledge_rag_url": "http://rag-demo/v1/experimental/demo/retrieve",
        "knowledge_rag_token": TOKEN,
        "knowledge_rag_corpus_sha256": PIN,
        **kw,
    }


def test_mapping_scope_fact_integrity_and_citation_membership(monkeypatch):
    calls = install(monkeypatch)

    async def check():
        async with runtime(**config()) as (c, _):
            ctx = context(c)
            result = await KnowledgeAdapter().execute(TASK, ARGS, ctx)
            ctx.record.results[TASK.task_id] = result
            assert result.status == "succeeded" and result.facts[0].fact.record_ref == REF
            assert result.evidence_ids[0] != str(uuid5(NAMESPACE_URL, "repo://demo/protocol-a"))
            assert await c.repository.verify_facts(SCOPE.scope_hash, result.facts)
            assert not await c.repository.verify_facts("other", result.facts)
            refs, _ = await audit_remote_answer(f"仅合成方案 [{REF}]", [], ctx)
            assert refs == [REF] and ctx.tool_calls == 1
            for text in ["没有引用", "伪造 [protocol-z@v1#outcomes]"]:
                with pytest.raises(DomainError, match="回答缺少有效来源"):
                    await audit_remote_answer(text, [], ctx)
            assert calls[0].headers["X-Tenant-ID"] == SCOPE.tenant_id

    asyncio.run(check())


def test_no_evidence_remains_unavailable(monkeypatch):
    install(monkeypatch, lambda b: b.update(status="no_evidence", evidence=[]))

    async def check():
        async with runtime(**config()) as (c, _):
            result = await KnowledgeAdapter().execute(TASK, ARGS, context(c))
            assert result.status == "unavailable" and result.code == "no_knowledge_evidence"
            assert not result.evidence_ids

    asyncio.run(check())


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "rag_unauthorized"),
        (403, "rag_scope_denied"),
        (409, "rag_version_unavailable"),
        (503, "rag_unavailable"),
        (500, "rag_service_failed"),
        (302, "rag_service_failed"),
    ],
)
def test_http_failure_classification_without_fallback(monkeypatch, status, code):
    calls = install(monkeypatch, status=status)

    async def check():
        async with runtime(**config()) as (c, _):
            ctx = context(c)
            with pytest.raises(DomainError) as exc:
                await KnowledgeAdapter().execute(TASK, ARGS, ctx)
            assert exc.value.code == code
            assert ctx.record.metrics["knowledge_retrievals"][0]["code"] == code
            assert len(calls) == 1

    asyncio.run(check())


@pytest.mark.parametrize(
    "error,code",
    [
        (httpx.ReadTimeout("injected"), "rag_timeout"),
        (httpx.ConnectError("injected"), "rag_connection_failed"),
    ],
)
def test_timeout_and_connection_errors(monkeypatch, error, code):
    install(monkeypatch, error=error)

    async def check():
        async with runtime(**config()) as (c, _):
            with pytest.raises(DomainError) as exc:
                await KnowledgeAdapter().execute(TASK, ARGS, context(c))
            assert exc.value.code == code and exc.value.retryable

    asyncio.run(check())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b["evidence"][0].update(doc_ref="forged@v1#outcomes"),
        lambda b: b["evidence"][0].update(text="changed"),
        lambda b: b["evidence"][0].update(page=1),
        lambda b: b["evidence"][0].update(score=float("inf")),
        lambda b: b.update(request_id=str(uuid4())),
        lambda b: b.update(corpus_sha256="b" * 64),
        lambda b: b.update(status="no_evidence"),
        lambda b: b.update(evidence=b["evidence"] * 2),
    ],
)
def test_signed_but_inconsistent_response_is_rejected(monkeypatch, mutation):
    install(monkeypatch, mutation)

    async def check():
        async with runtime(**config()) as (c, _):
            with pytest.raises(DomainError) as exc:
                await KnowledgeAdapter().execute(TASK, ARGS, context(c))
            assert exc.value.code == "rag_response_invalid"

    asyncio.run(check())


@pytest.mark.parametrize(
    "raw",
    [b'{"signature":"forged","evidence":[]}', b"x" * 65537, b"not json"],
    ids=["forged-signature", "oversized-body", "malformed-json"],
)
def test_signature_and_stream_size_limits(monkeypatch, raw):
    install(monkeypatch, raw=raw)

    async def check():
        async with runtime(**config()) as (c, _):
            with pytest.raises(DomainError) as exc:
                await KnowledgeAdapter().execute(TASK, ARGS, context(c))
            assert exc.value.code == "rag_response_invalid"

    asyncio.run(check())


@pytest.mark.parametrize(
    "scope",
    [
        TrustedScope("other", "actor", role="operator"),
        TrustedScope("integration-demo", "actor", role="patient"),
        TrustedScope("integration-demo", "actor", role="operator", patient_id="1"),
    ],
)
def test_scope_gate_precedes_network(monkeypatch, scope):
    calls = install(monkeypatch)

    async def check():
        async with runtime(**config()) as (c, _):
            with pytest.raises(DomainError) as exc:
                await KnowledgeAdapter().execute(TASK, ARGS, context(c, scope))
            assert exc.value.code == "rag_demo_scope_denied" and not calls

    asyncio.run(check())


def test_budget_and_expired_history_evidence(monkeypatch):
    calls = install(monkeypatch)

    async def check():
        async with runtime(**config()) as (c, _):
            ctx = context(c)
            ctx.tool_calls = ctx.settings.max_tool_calls
            with pytest.raises(DomainError) as exc:
                await KnowledgeAdapter().execute(TASK, ARGS, ctx)
            assert exc.value.code == "tool_budget" and not calls
            ctx.tool_calls = 0
            result = await KnowledgeAdapter().execute(TASK, ARGS, ctx)
            eid = result.evidence_ids[0]
            history = [
                ToolMessage(
                    content=json.dumps({"tool_name": "search_knowledge", "evidence_id": eid}),
                    tool_call_id="c",
                )
            ]
            assert (await audit_remote_answer(f"引用 [{REF}]", history, ctx))[0] == [REF]
            await c.repository.delete("evidence", SCOPE.scope_hash, eid)
            with pytest.raises(DomainError):
                await audit_remote_answer(f"引用 [{REF}]", history, ctx)

    asyncio.run(check())


def test_disabled_backend_never_connects(monkeypatch, tmp_path):
    calls = install(monkeypatch)
    from tests.test_knowledge_and_evidence import corpus

    path = tmp_path / "local.json"
    path.write_text(json.dumps(corpus()), encoding="utf-8")

    async def check():
        async with runtime(knowledge_corpus_path=str(path)) as (c, _):
            result = await KnowledgeAdapter().execute(
                TASK, {"query": "面板使用方法", "domain": "help"}, context(c)
            )
            assert result.status == "succeeded" and not calls

    asyncio.run(check())


@pytest.mark.parametrize(
    "change",
    [
        {"knowledge_rag_url": "https://example.invalid/wrong-path"},
        {"knowledge_rag_token": ""},
        {"knowledge_rag_corpus_sha256": ""},
    ],
)
def test_invalid_configuration_fails_before_network(monkeypatch, change):
    calls = install(monkeypatch)

    async def check():
        async with runtime(**config(**change)) as (c, _):
            with pytest.raises(DomainError) as exc:
                await KnowledgeAdapter().execute(TASK, ARGS, context(c))
            assert exc.value.code == "rag_configuration_invalid" and not calls

    asyncio.run(check())
