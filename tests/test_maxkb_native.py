"""
创建日期：2026-10-27
文件功能：验证MaxKB原生API直连、可信医院范围、发布原文和调用预算。
"""

import asyncio
import hashlib
import json

import httpx
import pytest
from pydantic import ValidationError

from meta_agent.config import Settings
from meta_agent.context.budget import estimate_tokens
from meta_agent.integrations.knowledge import (
    KnowledgeAccessDenied,
    KnowledgeContractError,
    KnowledgeSourceConfig,
    search,
    tool_schema,
)

KB = "11111111-1111-4111-8111-111111111111"
DOC = "22222222-2222-4222-8222-222222222222"
PARAGRAPH = "33333333-3333-4333-8333-333333333333"
SPACE = "医院A"
TEXT = "电脑端按空格键跳跃，按 Shift 键奔跑。"


@pytest.fixture
def source_data(tmp_path, monkeypatch):
    value = {
        "schema_version": 2,
        "routes": [
            {
                "knowledge_id": KB,
                "knowledge_type": "sop_kb",
                "enabled": True,
                "scope_kind": "space",
                "space_id": SPACE,
                "roles": ["operator"],
            }
        ],
        "strategies": {"sop_kb": {"mode": "blend"}},
        "documents": {
            DOC: {
                "id": DOC,
                "knowledge_id": KB,
                "published": True,
                "roles": ["operator"],
                "scope_kind": "space",
                "space_id": SPACE,
                "knowledge_type": "sop_kb",
                "code": "manual",
                "name": "操作说明",
                "revision_sha256": "a" * 64,
            }
        },
        "paragraphs": {
            PARAGRAPH: {
                "document_id": DOC,
                "knowledge_id": KB,
                "published": True,
                "text": TEXT,
                "title": "键盘操作",
                "page": 2,
            }
        },
    }
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    path = tmp_path / "catalog.json"
    path.write_bytes(raw)
    monkeypatch.setenv("MAXKB_TEST_USER", "native-user")
    monkeypatch.setenv("MAXKB_TEST_PASSWORD", "native-password")
    return {
        "id": "maxkb",
        "description": "原生MaxKB知识源",
        "protocol": "maxkb",
        "url": "http://maxkb.test:18081/admin/api",
        "domains": ["help"],
        "scoped_domains": ["help"],
        "maxkb": {
            "username_env": "MAXKB_TEST_USER",
            "password_env": "MAXKB_TEST_PASSWORD",
            "catalog_path": str(path),
            "catalog_sha256": hashlib.sha256(raw).hexdigest(),
            "allowed_space_ids": [SPACE],
        },
    }


def mock_native(monkeypatch, *, mode="ok"):
    seen = []

    def handle(request):
        seen.append(request)
        assert request.url.host == "maxkb.test" and request.url.port == 18081
        assert "bridge" not in str(request.url) and "adapter" not in str(request.url)
        if request.url.path.endswith("/user/login"):
            assert json.loads(request.content) == {
                "username": "native-user",
                "password": "native-password",
            }
            return httpx.Response(200, json={"code": 200, "data": {"token": "native-session"}})
        assert request.headers["Authorization"] == "Bearer native-session"
        if request.url.path.endswith("/hit_test"):
            if mode == "provider_failure":
                return httpx.Response(
                    200, json={"code": 500, "message": "do-not-log-provider-secret"}
                )
            if mode == "timeout":
                raise httpx.ReadTimeout("timeout")
            rows = (
                []
                if mode == "empty"
                else [
                    {
                        "id": PARAGRAPH,
                        "document_id": DOC,
                        "is_active": True,
                        "content": "changed text" if mode == "changed" else TEXT,
                        "comprehensive_score": 0.9,
                    }
                ]
            )
            return httpx.Response(200, json={"code": 200, "data": rows})
        if "/paragraph/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {"id": PARAGRAPH, "is_active": mode != "disabled", "content": TEXT},
                },
            )
        return httpx.Response(200, json={"code": 200, "data": {"id": DOC, "is_active": True}})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "meta_agent.integrations.maxkb_native.httpx.AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handle)),
    )
    return seen


def invoke(data, **changes):
    payload = {
        "query": "电脑端如何跳跃？",
        "domain": "help",
        "scope": {"space_id": SPACE},
        "limits": {"max_context_tokens": 4000},
    }
    payload.update(changes)
    return asyncio.run(
        search(KnowledgeSourceConfig(**data), payload, request_id="native-probe", max_bytes=4096)
    )


def test_calls_native_maxkb_only_and_preserves_sources(source_data, monkeypatch):
    seen = mock_native(monkeypatch)
    value = invoke(source_data)
    assert value["status"] == "ok" and TEXT in value["context"]
    assert value["sources"][0]["ref"] == "manual@v-aaaaaaaaaaaa#c-33333333333343338333333333333333"
    assert "2" in value["sources"][0]["locator"]
    assert len(seen) == 4 and seen[0].method == "POST"
    assert [r.method for r in seen] == ["POST", "POST", "GET", "GET"]
    assert "native-password" not in str(value) and "native-session" not in str(value)
    assert tool_schema([KnowledgeSourceConfig(**source_data)])["properties"]["source"]["enum"] == [
        "maxkb"
    ]


@pytest.mark.parametrize("space", [None, "另一家医院"])
def test_scope_denial_happens_before_login(source_data, monkeypatch, space):
    seen = mock_native(monkeypatch)
    with pytest.raises(KnowledgeAccessDenied):
        invoke(source_data, scope={} if space is None else {"space_id": space})
    assert not seen


def test_catalog_integrity_is_checked_before_network(source_data, monkeypatch):
    seen = mock_native(monkeypatch)
    source_data["maxkb"]["catalog_sha256"] = "0" * 64
    with pytest.raises(KnowledgeContractError, match="integrity"):
        invoke(source_data)
    assert not seen


@pytest.mark.parametrize("mode", ["changed", "provider_failure"])
def test_errors_do_not_become_empty_evidence_or_expose_provider_details(
    source_data, monkeypatch, mode
):
    mock_native(monkeypatch, mode=mode)
    with pytest.raises(KnowledgeContractError) as error:
        invoke(source_data)
    assert "do-not-log" not in str(error.value)


@pytest.mark.parametrize("mode", ["disabled", "empty"])
def test_disabled_or_missing_native_evidence_is_not_returned(source_data, monkeypatch, mode):
    mock_native(monkeypatch, mode=mode)
    value = invoke(source_data)
    assert value["status"] == "no_evidence" and not value["sources"] and not value["context"]


def test_native_timeout_is_not_retried(source_data, monkeypatch):
    seen = mock_native(monkeypatch, mode="timeout")
    with pytest.raises(httpx.ReadTimeout):
        invoke(source_data)
    assert len(seen) == 2


def test_small_budget_is_enforced(source_data, monkeypatch):
    mock_native(monkeypatch)
    value = invoke(source_data, limits={"max_context_tokens": 128})
    assert value["status"] in {"partial", "no_evidence"} and value["gaps"]
    assert estimate_tokens({"context": value["context"], "sources": value["sources"]}) <= 128


def test_native_credentials_can_be_configured_in_dotenv(source_data, tmp_path, monkeypatch):
    monkeypatch.delenv("MAXKB_TEST_USER")
    monkeypatch.delenv("MAXKB_TEST_PASSWORD")
    p = tmp_path / ".env"
    p.write_text(
        "META_AGENT__KNOWLEDGE_SOURCES="
        + json.dumps([source_data])
        + "\nMAXKB_TEST_USER=file-user\nMAXKB_TEST_PASSWORD=file-password\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=p)
    assert settings.knowledge_sources[0].maxkb.credentials() == ("file-user", "file-password")
    monkeypatch.setenv("MAXKB_TEST_PASSWORD", "process-password")
    assert Settings(_env_file=p).knowledge_sources[0].maxkb.credentials() == (
        "file-user",
        "process-password",
    )


def test_native_provider_requires_explicit_configuration_and_scope(source_data):
    with pytest.raises(ValidationError):
        KnowledgeSourceConfig(**{**source_data, "maxkb": None})
    with pytest.raises(ValidationError):
        KnowledgeSourceConfig(**{**source_data, "scoped_domains": []})
