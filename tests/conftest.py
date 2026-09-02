"""
创建日期：2026-08-29
文件功能：提供启用模拟 IREGO 结果和内存持久化的 FastAPI 测试客户端。
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from meta_agent.app import create_app
from meta_agent.config import Settings


@pytest.fixture
def settings() -> Settings:
    """构造不会访问真实患者数据的测试配置。"""
    return Settings(
        app_env="test",
        service_bearer_token="test-service-token",
        dry_run=True,
        persistence_backend="memory",
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
