"""
创建日期：2026-08-29
文件功能：提供启用模拟 IREGO 结果和内存持久化的 FastAPI 测试客户端。
"""

import ipaddress
import socket
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from meta_agent.app import create_app
from meta_agent.config import Settings
from tests.helpers.settings import AppTestSettings


@pytest.fixture(autouse=True)
def block_live_business_network(monkeypatch, request):
    """Offline suites must never log in to or read a real clinical service."""
    if "integration" in str(request.node.path):
        return
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def guard(original):
        def checked(sock, address):
            if isinstance(address, tuple):
                host = str(address[0])
                try:
                    local = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    local = host == "localhost"
                if not local:
                    raise AssertionError("Live network access is disabled in offline tests")
            return original(sock, address)

        return checked

    monkeypatch.setattr(socket.socket, "connect", guard(connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(connect_ex))


@pytest.fixture
def settings() -> Settings:
    """构造不会访问真实患者数据和本地 LLM 配置的测试配置。"""

    return AppTestSettings(
        app_env="test",
        service_bearer_token="test-service-token",
        dry_run=True,
        persistence_backend="memory",
        planner_mode="deterministic",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """启动并在测试结束后关闭应用生命周期。"""
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """提供可信服务调用头。"""
    return {"Authorization": "Bearer test-service-token"}
