from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


LLMProvider = Literal["openai", "qwen", "deepseek"]
AgentSessionBackend = Literal["redis", "memory"]


class ResolvedLLMConfig(BaseModel):
    provider: LLMProvider
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    tracing_enabled: bool = False

    @property
    def can_use_agents_sdk(self) -> bool:
        if not self.api_key:
            return False
        if self.provider in {"qwen", "deepseek"}:
            return bool(self.model and self.base_url)
        return True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "rehab-execution-deviation-demo"

    llm_provider: LLMProvider = "qwen"
    agents_tracing_enabled: bool = False

    agent_session_backend: AgentSessionBackend = "redis"
    agent_session_redis_url: str | None = "redis://127.0.0.1:6379/0"
    agent_session_redis_key_prefix: str = "metaagent:agents:session"
    agent_session_ttl_seconds: int | None = 60 * 60 * 24
    agent_session_history_limit: int | None = None

    openai_api_key: str | None = None
    openai_model: str | None = None
    openai_base_url: str | None = None

    qwen_api_key: str | None = None
    qwen_model: str = "qwen-plus"
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    deepseek_api_key: str | None = None
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_database: str = "meta_universe"
    mysql_user: str = "meta_user"
    mysql_password: str | None = None
    mysql_charset: str = "utf8mb4"
    mysql_connect_timeout: int = 5

    use_mock_when_db_unavailable: bool = True
    default_time_window_days: int = 30
    default_weekly_report_days: int = 7
    demo_default_therapist_id: int = 56
    demo_default_plan_id: int = 6
    demo_default_patient_id: int = 146

    high_risk_threshold: float = 75.0
    medium_risk_threshold: float = 45.0

    def resolve_llm_config(
        self,
        *,
        provider: LLMProvider | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> ResolvedLLMConfig:
        selected_provider = provider or self.llm_provider
        if selected_provider == "openai":
            return ResolvedLLMConfig(
                provider="openai",
                api_key=api_key or self.openai_api_key,
                model=model or self.openai_model,
                base_url=base_url or self.openai_base_url,
                tracing_enabled=self.agents_tracing_enabled,
            )
        if selected_provider == "qwen":
            return ResolvedLLMConfig(
                provider="qwen",
                api_key=api_key or self.qwen_api_key,
                model=model or self.qwen_model,
                base_url=base_url or self.qwen_base_url,
                tracing_enabled=False,
            )
        return ResolvedLLMConfig(
            provider="deepseek",
            api_key=api_key or self.deepseek_api_key,
            model=model or self.deepseek_model,
            base_url=base_url or self.deepseek_base_url,
            tracing_enabled=False,
        )

    @property
    def has_default_llm_credentials(self) -> bool:
        return self.resolve_llm_config().can_use_agents_sdk

    @property
    def has_database_credentials(self) -> bool:
        return bool(self.mysql_user and self.mysql_database and self.mysql_password is not None)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
