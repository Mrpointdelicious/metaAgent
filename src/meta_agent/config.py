"""
创建日期：2026-08-29
文件功能：定义环境变量配置、生产运行门和外部依赖参数。
"""

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from meta_agent.integrations.knowledge import KnowledgeSourceConfig


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
    orchestration_mode: Literal["agent", "legacy"] = "agent"
    prompt_bundle: str = "builtin-v1"
    agent_model: str = ""
    agent_max_output_tokens: int | None = Field(default=None, ge=128, le=8192)
    agent_max_model_calls: int = Field(default=8, ge=2, le=20)
    agent_model_timeout_seconds: float = Field(default=30, gt=0, le=120)
    agent_tool_output_tokens: int = Field(default=16000, ge=512, le=32000)
    agent_history_tokens: int = Field(default=24000, ge=1024, le=64000)

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
    request_timeout_seconds: float = Field(default=60, gt=0, le=180)
    tool_timeout_seconds: float = Field(default=15, gt=0, le=40)
    report_timeout_seconds: float = Field(default=40, gt=0, le=40)
    answer_timeout_seconds: float = Field(default=8, gt=0, le=30)
    planner_input_tokens: int = Field(default=6000, ge=512, le=16000)
    answer_input_tokens: int = Field(default=12000, ge=512, le=32000)
    model_context_tokens: int = Field(default=65536, ge=4096)
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
    knowledge_sources: list[KnowledgeSourceConfig] = Field(default_factory=list, max_length=20)
    knowledge_context_tokens: int = Field(default=4000, ge=128, le=16000)
    portfolio_enabled: bool = False
    portfolio_base_url: str = "http://rag-portfolio-api:8000"
    portfolio_token: SecretStr = SecretStr("")
    # Global medical literature; never selected by spaceId.
    portfolio_kb_ids: list[str] = Field(default_factory=list)
    # Trusted space -> hospital help/product -> existing RAG knowledge-base IDs.
    portfolio_space_kb_ids: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    portfolio_help_token: SecretStr = SecretStr("")
    portfolio_timeout_seconds: float = Field(default=20, gt=0, le=40)
    portfolio_top_k: int = Field(default=5, ge=1, le=20)
    portfolio_context_tokens: int = Field(default=4000, ge=128, le=16000)
    portfolio_scene_catalog_enabled: bool = False
    portfolio_scene_catalog_ttl_seconds: float = Field(default=30, ge=1, le=300)
    # Historical lab settings only; the current API/runtime never enables MaxKB.
    maxkb_enabled: bool = False
    maxkb_url: str = "http://host.docker.internal:18081/admin/api"
    maxkb_config_path: str = ""
    maxkb_public_base_url: str = "http://127.0.0.1:18084"
    maxkb_release_path: str = ""
    maxkb_timeout_seconds: float = Field(default=15, gt=0, le=45)
    maxkb_top_k: int = Field(default=3, ge=1, le=6)
    maxkb_context_budget: int = Field(default=10000, ge=100, le=12000)
    maxkb_answer_mode: Literal["llm", "extractive", "disabled"] = "llm"
    maxkb_answer_timeout_seconds: float = Field(default=30, gt=0, le=60)
    maxkb_answer_max_tokens: int = Field(default=768, ge=128, le=2048)
    artifact_allowed_hosts: list[str] = Field(default_factory=list)
    # Browser origin for local reports; check these artifacts over the internal backend network.
    artifact_public_base_url: str = ""
    # 患者姓名是否装入 LLM 上下文（关闭=姓名不装填，仅白名单其余字段；开启后仍受
    # “不得提及患者姓名”提示词约束）。默认关闭，联调阶段保持 false。
    patient_brief_include_name: bool = False
    multisource_patient_context_enabled: bool = False
    iretour_reports_enabled: bool = False
    irecog_reports_enabled: bool = False
    assessment_reports_enabled: bool = False
    ai_webapi_hospital_base_url: str = ""

    @field_validator("knowledge_sources")
    @classmethod
    def unique_knowledge_sources(cls, sources):
        if len({source.id for source in sources}) != len(sources):
            raise ValueError("knowledge source IDs must be unique")
        return sources

    @property
    def effective_agent_max_output_tokens(self) -> int:
        """Keep existing deployments unchanged until the Agent output limit is configured."""
        return (
            self.agent_max_output_tokens
            if self.agent_max_output_tokens is not None
            else self.planner_max_tokens
        )

    def runtime_issues(self) -> list[str]:
        issues = []
        if self.worker_count != 1 or os.environ.get("WEB_CONCURRENCY", "1") != "1":
            issues.append("v1仅支持一个worker；扩容须实现跨进程运行协调与事件通知")
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
        if (
            self.orchestration_mode == "agent"
            or self.planner_mode == "llm"
            or self.answer_mode == "llm_select"
        ) and not self.deepseek_api_key.get_secret_value():
            issues.append("启用模型时必须配置 META_AGENT__DEEPSEEK_API_KEY")
        if self.planner_max_retries != 0:
            issues.append("供应商自动重试须关闭，重试由统一预算管理")
        return issues


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """读取并缓存进程级配置。"""
    return Settings()
