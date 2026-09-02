"""
创建日期：2026-08-29
文件功能：定义环境变量配置、生产运行门和外部依赖参数。
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """MetaAgent 的强类型运行配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="META_AGENT__",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = "meta_agent"
    service_version: str = "0.1.0"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    service_bearer_token: str = ""

    ai_webapi_base_url: str = "http://host.docker.internal:5043/api/ai/patients/tools"
    ai_webapi_bearer_token: str = ""
    ai_webapi_timeout_seconds: float = Field(default=45.0, gt=0, le=300)
    dry_run: bool = True

    persistence_backend: Literal["memory", "postgres"] = "memory"
    postgres_dsn: str = ""
    auto_setup_persistence: bool = True
    evidence_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    default_tenant_id: str = "default"

    max_query_length: int = Field(default=4000, ge=128, le=32000)
    max_concurrent_requests: int = Field(default=100, ge=1, le=5000)

    def production_issues(self) -> list[str]:
        """返回会阻止生产环境安全启动的配置问题。"""
        issues: list[str] = []
        if self.app_env != "production":
            return issues
        if not self.service_bearer_token:
            issues.append("生产环境必须配置 META_AGENT__SERVICE_BEARER_TOKEN")
        if self.dry_run:
            issues.append("生产环境禁止启用 META_AGENT__DRY_RUN")
        if not self.ai_webapi_bearer_token:
            issues.append("生产环境必须配置 META_AGENT__AI_WEBAPI_BEARER_TOKEN")
        if self.persistence_backend != "postgres":
            issues.append("生产环境必须使用 PostgreSQL 持久化")
        if self.persistence_backend == "postgres" and not self.postgres_dsn:
            issues.append("PostgreSQL 持久化需要 META_AGENT__POSTGRES_DSN")
        return issues


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """读取并缓存进程级配置。"""
    return Settings()
