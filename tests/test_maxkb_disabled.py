"""
创建日期：2026-10-21
文件功能：验证旧 MaxKB 入口保持鉴权且无法通过历史配置重新启用。
"""

import pytest
from fastapi.testclient import TestClient

from meta_agent.app import create_app
from meta_agent.domains.maxkb import MaxKBWorkflow
from tests.helpers.settings import AppTestSettings


@pytest.mark.parametrize("maxkb_enabled", [False, True])
@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("endpoint", ["retrieve", "source"])
def test_legacy_maxkb_is_disabled_even_with_old_configuration(
    monkeypatch, maxkb_enabled, authorized, endpoint
):
    def forbidden_workflow(*args, **kwargs):
        pytest.fail("The current runtime must not construct the legacy MaxKB workflow")

    monkeypatch.setattr(MaxKBWorkflow, "__init__", forbidden_workflow)
    settings = AppTestSettings(
        service_bearer_token="test-service-token", maxkb_enabled=maxkb_enabled
    )
    headers = {"Authorization": "Bearer test-service-token"} if authorized else {}
    with TestClient(create_app(settings)) as client:
        assert not hasattr(client.app.state.container, "maxkb_workflow")
        if endpoint == "retrieve":
            reply = client.post(
                "/v1/knowledge/retrieve",
                headers=headers,
                json={"query": "检索指南", "tenant_id": "default", "space_id": "A"},
            )
        else:
            reply = client.get(
                "/v1/knowledge/sources/d-test",
                headers=headers,
                params={"tenant_id": "default", "space_id": "A"},
            )
    assert reply.status_code == (503 if authorized else 401)
    if authorized:
        assert reply.json() == {"detail": "maxkb_disabled"}
