"""
创建日期：2026-08-29
文件功能：验证生产环境缺少鉴权、真实后端或持久化配置时会被启动门拒绝。
"""

from meta_agent.config import Settings


def test_unsafe_production_configuration_is_rejected() -> None:
    settings = Settings(
        app_env="production",
        service_bearer_token="",
        ai_webapi_bearer_token="",
        dry_run=True,
        persistence_backend="memory",
    )

    issues = settings.production_issues()

    assert any("SERVICE_BEARER_TOKEN" in issue for issue in issues)
    assert any("DRY_RUN" in issue for issue in issues)
    assert any("AI_WEBAPI_BEARER_TOKEN" in issue for issue in issues)
    assert any("PostgreSQL" in issue for issue in issues)
