"""
创建日期：2026-08-29
文件功能：验证进程、就绪和外部依赖健康检查的基础契约。
"""

from fastapi.testclient import TestClient


def test_health_endpoints_are_available(client: TestClient) -> None:
    live = client.get("/health/live")
    ready = client.get("/health/ready")
    dependencies = client.get("/health/dependencies")

    assert live.status_code == 200
    assert live.json()["checks"]["process"] == "alive"
    assert ready.status_code == 200
    assert ready.json()["checks"]["graph"] == "compiled"
    assert ready.json()["checks"]["persistence"] == "memory"
    assert dependencies.status_code == 200
    assert dependencies.json()["checks"]["ai_webapi"] == "dry_run"
