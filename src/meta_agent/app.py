"""
创建日期：2026-08-29
文件功能：创建 FastAPI 应用，执行安全启动门并管理服务依赖生命周期。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from meta_agent.api.routes import router
from meta_agent.config import Settings, get_settings
from meta_agent.infrastructure.container import create_container
from meta_agent.observability.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造可注入配置的应用，便于测试和多环境部署。"""
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(
            resolved_settings.log_level,
            log_dir=resolved_settings.log_dir,
            log_file_name=resolved_settings.log_file_name,
            log_max_bytes=resolved_settings.log_max_bytes,
            log_backup_count=resolved_settings.log_backup_count,
        )
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
    # 浏览器前端直连本服务时，按配置的 Origin 返回 CORS 头，避免跨域请求被浏览器拦截。
    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_allow_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(router)
    return application


app = create_app()
