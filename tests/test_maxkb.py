"""
创建日期：2026-10-18
文件功能：验证 MaxKB 接入的引用、签名、流式元数据和可信空间边界。
"""

import hashlib
import hmac
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import pytest
from fastapi.testclient import TestClient

from meta_agent.app import create_app
from meta_agent.domains.maxkb import MaxKBWorkflow
from meta_agent.domains.maxkb_contract import Snippet, canonical, estimate_tokens
from meta_agent.orchestration.identity import TrustedScope
from tests.helpers.settings import TestSettings as IsolatedSettings


@pytest.fixture
def maxkb_settings(tmp_path):
    release = {
        "backend": "maxkb",
        "schema_version": 2,
        "scope_key": "maxkb-pilot",
        "space_ids": ["A"],
        "corpus_version": "maxkb-test",
        "corpus_sha256": "a" * 64,
        "index_sha256": "b" * 64,
    }
    path = tmp_path / "release.json"
    path.write_text(json.dumps(release), encoding="utf8")
    return IsolatedSettings(
        app_env="test",
        service_bearer_token="service-test",
        dry_run=True,
        persistence_backend="memory",
        planner_mode="deterministic",
        maxkb_enabled=True,
        maxkb_release_path=str(path),
        maxkb_url="http://bridge.test/v1/experimental/hybrid/retrieve",
        maxkb_token="k" * 40,
        maxkb_answer_mode="llm",
        deepseek_api_key="test-model-key",
    )


def transport(settings, calls, *, tamper=False):
    def handle(request):
        body = json.loads(request.content)
        calls.append(request.url.path)
        if request.url.path.endswith("/chat/completions"):
            rows = json.loads(body["messages"][-1]["content"])["evidence"]
            ref = rows[0]["doc_ref"]
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "answer": "按空格跳跃。[" + ref + "]",
                                        "citations": [ref],
                                    }
                                )
                            }
                        }
                    ]
                },
            )
        source, ref = "http://source.test/d-test?space_id=A", "d-test@v-test#c-test"
        text = "按空格跳跃，按Shift奔跑。"
        snippet = Snippet(
            source_id=uuid5(NAMESPACE_URL, source),
            chunk_id=uuid5(NAMESPACE_URL, source + "@v-test#c-test"),
            doc_ref=ref,
            document_version="v-test",
            title="操作说明",
            source=source,
            section="步骤",
            section_id="c-test",
            start=0,
            end=len(text),
            text=text,
            text_sha256=hashlib.sha256(text.encode()).hexdigest(),
            score=0.9,
            space_id="A",
            knowledge_type="sop_kb",
        ).model_dump(mode="json")
        release = json.loads(Path(settings.maxkb_release_path).read_text(encoding="utf8"))
        result = {
            "schema_version": "retrieval-v1",
            "request_id": body["request_id"],
            "retrieval_run_id": str(uuid4()),
            "backend": "maxkb",
            "strategy": "hybrid_selection",
            "approval": "technical_demo_only",
            "clinical_approval": False,
            "scope_key": "maxkb-pilot",
            "domain": body["domain"],
            **{k: release[k] for k in ("corpus_version", "corpus_sha256", "index_sha256")},
            "status": "success",
            "code": "ok",
            "elapsed_ms": 1,
            "reranker_used": None,
            "rerank_applied": False,
            "degradation": [],
            "candidate_count": 1,
            "post_rerank_count": 1,
            "selected_count": 1,
            "budget_truncated": False,
            "context_tokens_estimated": estimate_tokens(snippet),
            "model_calls": {"embedding": 1, "rerank": 0},
            "stages": {"space_id": "A"},
            "evidence": [snippet],
        }
        result["signature"] = hmac.new(b"k" * 40, canonical(result), hashlib.sha256).hexdigest()
        if tamper:
            result["evidence"][0]["text"] = "tampered"
        return httpx.Response(200, json=result)

    return httpx.MockTransport(handle)


def install(monkeypatch, settings, calls, **kwargs):
    original = MaxKBWorkflow.__init__

    def init(self, supplied):
        original(self, supplied)
        self.http = httpx.AsyncClient(transport=transport(settings, calls, **kwargs))

    monkeypatch.setattr(MaxKBWorkflow, "__init__", init)


def payload(space="A", mode="blocking"):
    return {
        "query": "请查询知识库，电脑端如何跳跃？",
        "user": "operator",
        "inputs": {"tenantId": "maxkb-pilot", "role": "operator", "space_id": space},
        "response_mode": mode,
    }


def test_graph_answers_with_scoped_citations_and_stream_metadata(maxkb_settings, monkeypatch):
    calls = []
    install(monkeypatch, maxkb_settings, calls)
    with TestClient(create_app(maxkb_settings)) as client:
        headers = {"Authorization": "Bearer service-test"}
        reply = client.post("/compat/dify/v1/chat-messages", headers=headers, json=payload())
        assert reply.status_code == 200
        value = reply.json()
        assert "按空格跳跃" in value["answer"] and "[d-test@v-test#c-test]" in value["answer"]
        assert value["metadata"]["knowledge_retrieval"]["response_verified"]
        assert value["metadata"]["citations"][0]["space_id"] == "A"
        stream = client.post(
            "/compat/dify/v1/chat-messages", headers=headers, json=payload(mode="streaming")
        )
        assert '"knowledge_retrieval"' in stream.text and '"status":"succeeded"' in stream.text
        assert len(calls) == 4


@pytest.mark.parametrize("space,status", [(None, 422), ("B", 403)])
def test_scope_rejected_before_outbound_request(maxkb_settings, monkeypatch, space, status):
    calls = []
    install(monkeypatch, maxkb_settings, calls)
    with TestClient(create_app(maxkb_settings)) as client:
        reply = client.post(
            "/compat/dify/v1/chat-messages",
            headers={"Authorization": "Bearer service-test"},
            json=payload(space),
        )
        assert reply.status_code == status and not calls
    assert (
        TrustedScope("t", "u", space_id="A").scope_hash
        != TrustedScope("t", "u", space_id="B").scope_hash
    )


def test_tampered_evidence_cannot_reach_answer_model(maxkb_settings, monkeypatch):
    calls = []
    install(monkeypatch, maxkb_settings, calls, tamper=True)
    with TestClient(create_app(maxkb_settings)) as client:
        reply = client.post(
            "/compat/dify/v1/chat-messages",
            headers={"Authorization": "Bearer service-test"},
            json=payload(),
        )
        assert reply.status_code == 502
        assert reply.json()["detail"] == "maxkb_evidence_invalid"
        assert calls == ["/v1/experimental/hybrid/retrieve"]
