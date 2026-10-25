"""
创建日期：2026-10-18
文件功能：保留旧 MaxKB 实验测试；已禁用的运行链路用例明确跳过。
"""

import hashlib
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from meta_agent.app import create_app
from meta_agent.domains.maxkb import MaxKBWorkflow
from meta_agent.domains.maxkb_contract import canonical
from meta_agent.domains.maxkb_policy import STRATEGIES
from tests.helpers.settings import TestSettings as IsolatedSettings

HEADERS = {"Authorization": "Bearer service-test"}
legacy_runtime = pytest.mark.skip(
    reason="旧 MaxKB 运行链路已禁用；当前链路使用原生 /v1/chat + rag-portfolio。"
)


def load_tool(name):
    path = Path(__file__).resolve().parents[1] / f"tools/{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sample(tmp_path):
    kb, other, doc_id, para_id = [str(uuid4()) for _ in range(4)]
    text = "按空格跳跃，按Shift奔跑。"
    original = tmp_path / "originals"
    original.mkdir()
    (original / "guide.txt").write_text(text, encoding="utf8")
    sha = hashlib.sha256(text.encode()).hexdigest()
    route = {
        "knowledge_id": kb,
        "enabled": True,
        "scope_kind": "space",
        "space_id": "A",
        "knowledge_type": "sop_kb",
        "roles": ["operator"],
    }
    doc = {
        "id": doc_id,
        "knowledge_id": kb,
        "name": "操作说明",
        "code": "d-test",
        "published": True,
        "scope_kind": "space",
        "space_id": "A",
        "roles": ["operator"],
        "knowledge_type": "sop_kb",
        "original_file": "guide.txt",
        "sha256": sha,
        "revision_sha256": sha,
        "group": "fixture",
    }
    paragraph = {
        "knowledge_id": kb,
        "document_id": doc_id,
        "text": text,
        "title": "步骤",
        "published": True,
        "page": 2,
    }
    catalog = {
        "schema_version": 2,
        "strategies": STRATEGIES,
        "routes": [route, {**route, "space_id": "B", "knowledge_id": other}],
        "documents": {doc_id: doc},
        "paragraphs": {para_id: paragraph},
    }
    release = {
        "schema_version": 2,
        "backend": "maxkb",
        "scope_key": "maxkb-pilot",
        "space_ids": ["A"],
        "corpus_version": "maxkb-test",
        "catalog_file": "catalog.json",
        "corpus_sha256": hashlib.sha256(canonical(catalog["documents"])).hexdigest(),
        "index_sha256": hashlib.sha256(canonical(catalog)).hexdigest(),
    }
    (tmp_path / "catalog.json").write_text(json.dumps(catalog), encoding="utf8")
    (tmp_path / "release.json").write_text(json.dumps(release), encoding="utf8")
    (tmp_path / "native.json").write_text(
        json.dumps({"username": "test", "password": "fake"}), encoding="utf8"
    )
    settings = IsolatedSettings(
        app_env="test",
        service_bearer_token="service-test",
        dry_run=True,
        persistence_backend="memory",
        planner_mode="deterministic",
        maxkb_enabled=True,
        maxkb_release_path=str(tmp_path / "release.json"),
        maxkb_config_path=str(tmp_path / "native.json"),
        maxkb_url="http://maxkb.test/admin/api",
        maxkb_answer_mode="llm",
        deepseek_api_key="fake-answer-key",
    )
    return {
        "settings": settings,
        "root": tmp_path,
        "catalog": catalog,
        "release": release,
        "kb": kb,
        "other": other,
        "doc": doc,
        "para": para_id,
        "text": text,
        "calls": [],
    }


def publish(sample):
    root, catalog, release = sample["root"], sample["catalog"], sample["release"]
    release["corpus_sha256"] = hashlib.sha256(canonical(catalog["documents"])).hexdigest()
    release["index_sha256"] = hashlib.sha256(canonical(catalog)).hexdigest()
    (root / "catalog.json").write_text(json.dumps(catalog), encoding="utf8")
    (root / "release.json").write_text(json.dumps(release), encoding="utf8")


def install(
    monkeypatch,
    sample,
    *,
    tamper=False,
    disabled=False,
    expired=False,
    fail=False,
    rerank=False,
    bad_rerank=None,
    disable_during_rerank=False,
):
    calls = sample["calls"]
    stale = [expired]
    enabled = [not disabled]
    if rerank:
        (sample["root"] / "native.json").write_text(
            json.dumps(
                {
                    "username": "test",
                    "password": "fake",
                    "rerank": {
                        "enabled": True,
                        "api_base": "http://maxkb.test/chat/api",
                        "api_key": "agent-fake-native",
                        "application_id": str(uuid4()),
                    },
                }
            ),
            encoding="utf8",
        )

    def handle(request):
        path = request.url.path
        calls.append(path)
        assert request.url.host in {"maxkb.test", "api.deepseek.com"}
        if path == "/chat/api/open":
            assert request.headers["Authorization"] == "Bearer agent-fake-native"
            return httpx.Response(200, json={"code": 200, "data": str(uuid4())})
        if path.startswith("/chat/api/chat_message/"):
            if disable_during_rerank:
                enabled[0] = False
            if bad_rerank == "provider":
                return httpx.Response(500, json={"code": 500, "message": "private-provider-detail"})
            body = json.loads(request.content)
            form = body["form_data"]
            value = {
                "schema_version": "maxkb-rerank-v1",
                "request_id": form["request_id"],
                "results": [
                    {"metadata": {**c, "relevance_score": 0.95}} for c in form["candidates"][:6]
                ],
            }
            if bad_rerank == "identity":
                value["results"][0]["metadata"]["id"] = "foreign-paragraph"
            if bad_rerank == "request":
                value["request_id"] = "another-request"
            return httpx.Response(200, json={"code": 200, "data": {"content": json.dumps(value)}})
        if path.endswith("/user/login"):
            return httpx.Response(200, json={"code": 200, "data": {"token": "fake-session"}})
        if path.endswith("/chat/completions"):
            body = json.loads(request.content)
            ref = json.loads(body["messages"][-1]["content"])["evidence"][0]["doc_ref"]
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "answer": f"按空格跳跃。[{ref}]",
                                        "citations": [ref],
                                    }
                                )
                            }
                        }
                    ]
                },
            )
        if stale[0]:
            stale[0] = False
            return httpx.Response(401, json={"code": 401})
        if fail:
            return httpx.Response(500, json={"code": 500, "message": "private-provider-detail"})
        if path.endswith("/hit_test"):
            assert sample["other"] not in path
            row = {
                "id": sample["para"],
                "document_id": sample["doc"]["id"],
                "content": "tampered" if tamper else sample["text"],
                "title": "步骤",
                "is_active": True,
                "comprehensive_score": 0.9,
            }
            data = [row]
        elif path.endswith("/paragraph"):
            data = [
                {
                    "id": sample["para"],
                    "content": sample["text"],
                    "title": "步骤",
                    "is_active": True,
                }
            ]
        else:
            data = {"id": sample["doc"]["id"], "is_active": enabled[0]}
        return httpx.Response(200, json={"code": 200, "data": data})

    original = MaxKBWorkflow.__init__

    def init(self, settings):
        original(self, settings)
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.retriever.http = self.http
        self.retriever.client.http = self.http

    monkeypatch.setattr(MaxKBWorkflow, "__init__", init)


def payload(space="A", mode="blocking"):
    return {
        "query": "请检索知识库，电脑端如何跳跃？",
        "user": "operator",
        "inputs": {"tenantId": "maxkb-pilot", "role": "operator", "space_id": space},
        "response_mode": mode,
    }


def query():
    return {"query": "电脑端如何跳跃？", "tenant_id": "maxkb-pilot", "space_id": "A"}


@legacy_runtime
def test_native_dify_answer_and_stream_smoke(sample, monkeypatch):
    install(monkeypatch, sample)
    with TestClient(create_app(sample["settings"])) as client:
        reply = client.post("/compat/dify/v1/chat-messages", headers=HEADERS, json=payload())
        assert reply.status_code == 200
        body = reply.json()
        assert "按空格跳跃" in body["answer"]
        assert body["metadata"]["knowledge_retrieval"]["integration"] == "native"
        assert body["metadata"]["citations"][0]["page"] == 2
        stream = client.post(
            "/compat/dify/v1/chat-messages", headers=HEADERS, json=payload(mode="streaming")
        )
        assert '"knowledge_retrieval"' in stream.text and '"status":"succeeded"' in stream.text
    assert sample["calls"].count("/admin/api/user/login") == 1
    assert all("hybrid/retrieve" not in c for c in sample["calls"])


@pytest.mark.parametrize("space,status", [(None, 422), ("B", 403)])
@legacy_runtime
def test_scope_rejected_before_any_outbound(sample, monkeypatch, space, status):
    install(monkeypatch, sample)
    with TestClient(create_app(sample["settings"])) as client:
        reply = client.post("/compat/dify/v1/chat-messages", headers=HEADERS, json=payload(space))
        assert reply.status_code == status
    assert not sample["calls"]


@legacy_runtime
def test_retrieval_only_and_authenticated_source(sample, monkeypatch):
    sample["settings"].maxkb_answer_mode = "disabled"
    sample["settings"].deepseek_api_key = ""
    install(monkeypatch, sample)
    with TestClient(create_app(sample["settings"])) as client:
        reply = client.post("/v1/knowledge/retrieve", headers=HEADERS, json=query())
        assert reply.status_code == 200
        evidence = reply.json()["evidence"][0]
        source = evidence["source"].replace("http://127.0.0.1:18084", "")
        assert client.get(source).status_code == 401
        assert client.get(source, headers=HEADERS).text == sample["text"]
        assert (
            client.get(source.replace("space_id=A", "space_id=B"), headers=HEADERS).status_code
            == 403
        )
        smoke = load_tool("smoke_maxkb")
        access = {
            "release": "maxkb-test",
            "tenant_id": "maxkb-pilot",
            "space_id": "A",
            "service_token": "service-test",
            "retrieve_url": "http://127.0.0.1:18084/v1/knowledge/retrieve",
        }
        assert smoke.check(client, access, "sop")["rerank_applied"] is False
        assert smoke.check(client, access, "scope")["http_status"] == 403
    assert not any("completions" in c for c in sample["calls"])


@legacy_runtime
def test_changed_evidence_cannot_reach_answer_model(sample, monkeypatch):
    install(monkeypatch, sample, tamper=True)
    with TestClient(create_app(sample["settings"])) as client:
        reply = client.post("/compat/dify/v1/chat-messages", headers=HEADERS, json=payload())
        assert reply.status_code == 409
    assert not any("completions" in c for c in sample["calls"])


@legacy_runtime
def test_disabled_document_not_recalled_or_downloadable(sample, monkeypatch):
    install(monkeypatch, sample, disabled=True)
    with TestClient(create_app(sample["settings"])) as client:
        reply = client.post("/v1/knowledge/retrieve", headers=HEADERS, json=query())
        assert reply.json()["evidence"] == []
        source = "/v1/knowledge/sources/d-test?tenant_id=maxkb-pilot&space_id=A"
        assert client.get(source, headers=HEADERS).status_code == 404


@legacy_runtime
def test_expired_native_session_recovers_once(sample, monkeypatch):
    install(monkeypatch, sample, expired=True)
    with TestClient(create_app(sample["settings"])) as client:
        assert (
            client.post("/v1/knowledge/retrieve", headers=HEADERS, json=query()).status_code == 200
        )
    assert sample["calls"].count("/admin/api/user/login") == 2


def clinical(sample):
    for route in sample["catalog"]["routes"]:
        route["knowledge_type"] = "clinical_kb"
    sample["doc"]["knowledge_type"] = "clinical_kb"
    publish(sample)


@legacy_runtime
def test_required_rerank_needs_native_configuration(sample, monkeypatch):
    clinical(sample)
    install(monkeypatch, sample)
    with TestClient(create_app(sample["settings"])) as client:
        reply = client.post(
            "/v1/knowledge/retrieve", headers=HEADERS, json={**query(), "domain": "health"}
        )
        body = reply.json()
        assert reply.status_code == 503 and body["detail"] == "maxkb_rerank_not_configured"
    assert not any(c.startswith("/chat/") for c in sample["calls"])


@legacy_runtime
def test_native_workflow_rerank_and_new_sessions(sample, monkeypatch):
    clinical(sample)
    install(monkeypatch, sample, rerank=True)
    with TestClient(create_app(sample["settings"])) as client:
        for _ in range(2):
            reply = client.post(
                "/v1/knowledge/retrieve", headers=HEADERS, json={**query(), "domain": "health"}
            )
            body = reply.json()
            assert reply.status_code == 200 and body["rerank_applied"]
            assert body["stages"]["native_rerank_requests"] == 1
            assert body["evidence"][0]["page"] == 2
            assert body["evidence"][0]["text"] == sample["text"]
    assert sample["calls"].count("/chat/api/open") == 2
    sessions = [p for p in sample["calls"] if p.startswith("/chat/api/chat_message/")]
    assert len(set(sessions)) == 2


@legacy_runtime
def test_document_disabled_during_rerank_is_not_returned(sample, monkeypatch):
    clinical(sample)
    install(monkeypatch, sample, rerank=True, disable_during_rerank=True)
    with TestClient(create_app(sample["settings"])) as client:
        reply = client.post(
            "/v1/knowledge/retrieve", headers=HEADERS, json={**query(), "domain": "health"}
        )
        body = reply.json()
        assert reply.status_code == 200 and body["evidence"] == []
        assert body["rerank_applied"] is False
        assert body["stages"]["native_rerank_requests"] == 1


@pytest.mark.parametrize("failure,status", [("identity", 502), ("request", 502), ("provider", 503)])
@legacy_runtime
def test_native_rerank_rejects_failed_or_foreign_results(sample, monkeypatch, failure, status):
    clinical(sample)
    install(monkeypatch, sample, rerank=True, bad_rerank=failure)
    with TestClient(create_app(sample["settings"])) as client:
        reply = client.post(
            "/v1/knowledge/retrieve", headers=HEADERS, json={**query(), "domain": "health"}
        )
        assert reply.status_code == status
        assert "private-provider-detail" not in reply.text
    assert not any("completions" in c for c in sample["calls"])


@legacy_runtime
def test_provider_failure_is_not_empty_retrieval_or_leaked(sample, monkeypatch):
    install(monkeypatch, sample, fail=True)
    with TestClient(create_app(sample["settings"])) as client:
        reply = client.post("/v1/knowledge/retrieve", headers=HEADERS, json=query())
        assert reply.status_code == 503 and "private-provider-detail" not in reply.text


def test_catalog_tampering_blocks_startup(sample):
    (sample["root"] / "catalog.json").write_text("{}", encoding="utf8")
    with pytest.raises(RuntimeError, match="integrity"):
        MaxKBWorkflow(sample["settings"])


def test_prepare_keeps_cleared_key_and_no_provider_credentials(sample):
    module = load_tool("prepare_maxkb_dev")
    root, output = sample["root"], sample["root"] / "output"
    (root / "access.json").write_text(
        json.dumps({"username": "test", "password": "fake"}), encoding="utf8"
    )
    (root / "agent.env").write_text("META_AGENT__DEEPSEEK_API_KEY=must-not-copy", encoding="utf8")
    result = module.prepare(
        root,
        output,
        maxkb_url="http://maxkb:8080/admin/api",
        public_url="http://127.0.0.1:18084",
        answer_mode="disabled",
        data_volume="test",
    )
    assert result["answer_key_configured"] is False
    assert "must-not-copy" not in (output / "agent.env").read_text("utf8")
    assert set(json.loads((output / "native.json").read_text("utf8"))) == {"username", "password"}
    native = json.loads((output / "native.json").read_text("utf8"))
    native["rerank"] = {
        "enabled": True,
        "api_base": "http://previous/chat/api",
        "api_key": "agent-test-only",
        "application_id": str(uuid4()),
    }
    (output / "native.json").write_text(json.dumps(native), encoding="utf8")
    module.prepare(
        root,
        output,
        maxkb_url="http://maxkb:8080/admin/api",
        public_url="http://127.0.0.1:18084",
        answer_mode="disabled",
        data_volume="test",
    )
    saved = json.loads((output / "native.json").read_text("utf8"))["rerank"]
    assert saved["api_key"] == "agent-test-only"
    assert saved["api_base"] == "http://maxkb:8080/chat/api"
