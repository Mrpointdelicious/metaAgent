"""
创建日期：2026-08-29
文件功能：创建 FastAPI 应用，执行安全启动门并管理服务依赖生命周期。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from meta_agent.api.routes import router
from meta_agent.config import Settings, get_settings
from meta_agent.infrastructure.container import create_container
from meta_agent.observability.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造可注入配置的应用，便于测试和多环境部署。"""
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(resolved_settings.log_level)
        issues = resolved_settings.production_issues()
        if issues:
            raise RuntimeError("；".join(issues))
        container = await create_container(resolved_settings)
        app.state.container = container
        try:
            yield
        finally:
            await container.close()

    application = FastAPI(
        title="MetaAgent Rehabilitation Orchestration Service",
        version=resolved_settings.service_version,
        lifespan=lifespan,
    )
    application.include_router(router)
    return application


app = create_app()
