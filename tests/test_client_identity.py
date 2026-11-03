"""
创建日期：2026-10-21
文件功能：验证第二个前端 Bearer Token 映射客户端身份，并按客户端排除工具（去挂号）。
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from meta_agent.agent.tools import tool_allowed
from meta_agent.api.security import ClientIdentity, require_service_identity
from tests.helpers.settings import AppTestSettings


def _request(token: str | None, settings) -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/chat-messages",
        "headers": headers,
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "scheme": "http",
        "app": SimpleNamespace(
            state=SimpleNamespace(container=SimpleNamespace(settings=settings))
        ),
    }
    return Request(scope)


def test_require_service_identity_maps_tokens_to_clients():
    settings = AppTestSettings(
        service_bearer_token="primary-token",
        service_bearer_token_secondary="secondary-token",
    )
    primary = asyncio.run(require_service_identity(_request("primary-token", settings)))
    assert primary.name == "primary"
    assert primary.excluded_tools == frozenset()

    secondary = asyncio.run(require_service_identity(_request("secondary-token", settings)))
    assert secondary.name == "secondary"
    assert secondary.excluded_tools == frozenset({"search_doctors"})


def test_require_service_identity_rejects_unknown_token():
    settings = AppTestSettings(
        service_bearer_token="primary-token",
        service_bearer_token_secondary="secondary-token",
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_service_identity(_request("unknown-token", settings)))
    assert exc.value.status_code == 401


def _ctx(client: ClientIdentity) -> SimpleNamespace:
    return SimpleNamespace(
        client=client,
        settings=SimpleNamespace(
            iretour_reports_enabled=False,
            scene_enabled=True,
            scene_actions_enabled=False,
            doctors_enabled=True,
            knowledge_backend="maxkb",
        ),
        scope=SimpleNamespace(
            project_patient_id="3799", iretour_patient_id=None, patient_id=None, role="patient"
        ),
        record=SimpleNamespace(raw_inputs={}),
    )


def test_tool_allowed_excludes_search_doctors_only_for_secondary():
    secondary = _ctx(ClientIdentity("secondary", frozenset({"search_doctors"})))
    assert tool_allowed(secondary, "search_doctors") is False
    # 其余工具组不受影响。
    assert tool_allowed(secondary, "search_knowledge") is True
    assert tool_allowed(secondary, "get_patient_profile") is True

    primary = _ctx(ClientIdentity("primary"))
    assert tool_allowed(primary, "search_doctors") is True
