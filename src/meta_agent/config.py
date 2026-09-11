"""
创建日期：2026-08-29
文件功能：定义环境变量配置、生产运行门和外部依赖参数。
"""

from functools import lru_cache
import os
from typing import Literal

from pydantic import Field, SecretStr
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
    service_version: str = "1.0.0"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    service_bearer_token: str = ""

    ai_webapi_base_url: str = "http://host.docker.internal:5043/api/ai/patients/tools"
    ai_webapi_bearer_token: str = ""
    ai_webapi_timeout_seconds: float = Field(default=45.0, gt=0, le=300)
    dry_run: bool = True

    persistence_backend: Literal["memory", "postgres"] = "memory"
    postgres_dsn: str = ""
    auto_setup_persistence: bool = False
    evidence_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    default_tenant_id: str = "default"

    # LLM / Planner
    planner_mode: Literal[
        "deterministic",
        "llm",
    ] = "deterministic"

    planner_model: str = "deepseek:deepseek-chat"

    planner_temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
    )

    planner_max_tokens: int = Field(
        default=2048,
        ge=128,
        le=8192,
    )

    planner_timeout_seconds: float = Field(
        default=8.0,
        gt=0,
        le=300,
    )

    planner_max_retries: int = Field(
        default=0,
        ge=0,
        le=10,
    )

    # DeepSeek
    deepseek_api_key: SecretStr = SecretStr("")

    deepseek_base_url: str = "https://api.deepseek.com"

    max_query_length: int = Field(default=4000, ge=128, le=32000)
    max_concurrent_requests: int = Field(default=20, ge=1, le=100)
    max_parallel_tasks: int = Field(default=4, ge=1, le=8)
    max_concurrent_tools: int = Field(default=8, ge=1, le=32)
    max_goals: int = Field(default=6, ge=1, le=6)
    max_tasks: int = Field(default=8, ge=1, le=8)
    max_tool_calls: int = Field(default=16, ge=1, le=64)
    max_llm_calls: int = Field(default=4, ge=1, le=4)
    request_timeout_seconds: float = Field(default=60, gt=0, le=60)
    tool_timeout_seconds: float = Field(default=15, gt=0, le=40)
    report_timeout_seconds: float = Field(default=40, gt=0, le=40)
    answer_timeout_seconds: float = Field(default=8, gt=0, le=30)
    planner_input_tokens: int = Field(default=4000, ge=512, le=16000)
    answer_input_tokens: int = Field(default=12000, ge=512, le=32000)
    model_context_tokens: int = Field(default=32768, ge=4096)
    answer_mode: Literal["template", "llm_select"] = "template"
    answer_model: str = "deepseek:deepseek-chat"
    conversation_ttl_seconds: int = Field(default=86400, ge=60, le=86400)
    cache_ttl_seconds: int = Field(default=300, ge=0, le=300)
    doctor_ttl_seconds: int = Field(default=30, ge=0, le=30)
    run_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    audit_ttl_seconds: int = Field(default=604800, ge=60, le=604800)
    cleanup_interval_seconds: float = Field(default=3600, gt=0, le=3600)
    worker_count: int = 1
    ai_webapi_rehab_base_url: str = ""
    ai_webapi_health_url: str = ""
    max_tool_response_bytes: int = Field(default=2_000_000, ge=4096, le=20_000_000)
    scene_enabled: bool = True
    doctors_enabled: bool = True
    scene_actions_enabled: bool = False
    knowledge_corpus_path: str = ""
    artifact_allowed_hosts: list[str] = Field(default_factory=list)

    def runtime_issues(self) -> list[str]:
        issues = []
        if self.worker_count != 1 or os.environ.get("WEB_CONCURRENCY", "1") != "1":
            issues.append("v1仅支持一个worker；扩容须实现跨进程租约与事件回放")
        return issues

    def production_issues(self) -> list[str]:
        """返回会阻止生产环境安全启动的配置问题。"""
        issues: list[str] = self.runtime_issues()
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
        if self.auto_setup_persistence:
            issues.append("生产环境须先运行迁移，AUTO_SETUP_PERSISTENCE必须关闭")
        if self.planner_mode == "llm":
            if not self.deepseek_api_key.get_secret_value():
                issues.append(
                    "启用 LLM Planner 时必须配置 "
                    "META_AGENT__DEEPSEEK_API_KEY"
                )
        return issues


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """读取并缓存进程级配置。"""
    return Settings()
