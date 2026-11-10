"""
创建日期：2026-08-29
文件功能：定义环境变量配置、生产运行门和外部依赖参数。
"""

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from meta_agent.integrations.knowledge import KnowledgeSourceConfig


class Settings(BaseSettings):
    """MetaAgent 的强类型运行配置。"""

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_prefix="META_AGENT__",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = "meta_agent"
    service_version: str = "1.0.0"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    # 结构化 JSON 日志落盘目录；为空时仅输出到 stdout（终端 / Docker 日志）。
    # 配置后会在该目录下按 log_file_name 写滚动文件，便于事后排查。
    log_dir: str = ""
    log_file_name: str = "meta_agent.log"
    log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    log_backup_count: int = Field(default=5, ge=0, le=100)
    service_bearer_token: str = ""
    # 第二个前端的独立 Bearer Token（可选）；命中后按 secondary 客户端处理，
    # 工具可见性受 secondary_excluded_tools 约束（默认去掉挂号）。
    service_bearer_token_secondary: str = ""
    # secondary 客户端禁用的工具名集合（默认去掉挂号 search_doctors）。
    secondary_excluded_tools: list[str] = Field(default_factory=lambda: ["search_doctors"])
    # 允许跨域访问的前端来源（Origin）。浏览器直连本服务时，据此返回 CORS 头；
    # 默认 "*" 放行所有来源。生产环境建议收敛为具体前端地址，JSON 数组格式，
    # 如 ["https://metaverse.example.com"]。
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"])

    ai_webapi_base_url: str = "http://host.docker.internal:5043/api/ai/patients/tools"
    ai_webapi_bearer_token: str = ""
    ai_webapi_timeout_seconds: float = Field(default=45.0, gt=0, le=300)
    dry_run: bool = True
    orchestration_mode: Literal["agent", "legacy"] = "agent"
    agent_model: str = ""
    agent_max_model_calls: int = Field(default=8, ge=2, le=20)
    agent_model_timeout_seconds: float = Field(default=30, gt=0, le=120)
    agent_tool_output_tokens: int = Field(default=3000, ge=512, le=8000)
    agent_history_tokens: int = Field(default=8000, ge=1024, le=20000)
    # 自定义 Agent 系统提示词；为空时使用内置 SYSTEM_PROMPT。
    # 设置后整体替换内置提示词，便于不重建镜像就调整回答边界/风格。
    agent_system_prompt: str = ""

    persistence_backend: Literal["memory", "postgres"] = "memory"
    postgres_dsn: str = ""
    auto_setup_persistence: bool = False
    # postgres 连接超时（秒）。Windows Docker 下 postgres 磁盘 IO 慢、checkpoint 高峰时
    # 单连接握手可达 2s+，启动并发建多个连接时 5s 会不够导致 ConnectionTimeout 启动失败。
    postgres_connect_timeout_seconds: int = Field(default=30, ge=5, le=120)
    evidence_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    default_tenant_id: str = "default"
    # 旧会话索引未记录接入方。仅声明原接入方后才迁移可核验的普通会话。
    legacy_conversation_client: Literal["", "primary", "secondary"] = ""

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
    model_context_tokens: int = Field(default=32768, ge=4096)
    answer_mode: Literal["template", "llm_select"] = "template"
    answer_model: str = "deepseek:deepseek-chat"
    conversation_ttl_seconds: int = Field(default=604800, ge=60, le=31536000)
    # 会话历史持久化的最大轮数（每轮 = 用户问 + 助手答各一条）。/v1/messages 读这份数据，
    # 之前硬编码 6 轮导致历史会话只显示最近几条；读取端的 agent 上下文仍各自截断，不受此影响。
    conversation_turn_limit: int = Field(default=100, ge=1, le=500)
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
    # search_knowledge 仅使用统一契约；空列表表示尚未接通知识服务。
    knowledge_sources: list[KnowledgeSourceConfig] = Field(default_factory=list, max_length=20)
    # 兼容旧部署的占位配置，不再决定知识检索的实现。
    knowledge_backend: Literal["maxkb"] = "maxkb"
    knowledge_context_tokens: int = Field(default=4000, ge=128, le=16000)
    # 仅用于发现尚未迁移的旧配置，不再启用任何本地/Portfolio 检索逻辑。
    knowledge_corpus_path: str = ""
    portfolio_enabled: bool = False
    portfolio_scene_catalog_enabled: bool = False
    artifact_allowed_hosts: list[str] = Field(default_factory=list)
    # 制品 URL 反代前缀归一化：后端返回的 image_url 用内部路径 /api/ai/...，
    # 而公开入口是 AI_WEBAPI_*_BASE_URL 里的 /aiwebapi/ai/...（差一个反代前缀）。
    # 非空时把 image_url 首段路径 /api/ 替换为该前缀（如 aiwebapi），使 [Cweb] 展示
    # 和 artifact_available 可用性检查都指向公开地址；留空则不重写（原样透传）。
    ai_webapi_artifact_path_prefix: str = ""
    # 患者姓名是否装入 LLM 上下文（关闭=姓名不装填，仅白名单其余字段；开启后仍受
    # “不得提及患者姓名”提示词约束）。默认关闭，联调阶段保持 false。
    patient_brief_include_name: bool = False
    multisource_patient_context_enabled: bool = False
    iretour_reports_enabled: bool = False
    irecog_reports_enabled: bool = False
    assessment_reports_enabled: bool = False
    ai_webapi_hospital_base_url: str = ""
    # 医院运营接口可单独指向另一套后端（如 5043），其鉴权 token 与此字段覆盖；
    # 为空时沿用 ai_webapi_bearer_token。
    ai_webapi_hospital_bearer_token: str = ""
    # webyun 直连业务接口的固定服务账号（场景指令 / 医生 / 问诊房间共用的登录凭据）。
    webyun_login_phone: str = ""
    webyun_login_password: SecretStr = SecretStr("")

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        file_values = dotenv_settings()

        def with_file_tokens(source):
            def read():
                values = source()
                configured = values.get("knowledge_sources")
                if isinstance(configured, list):
                    resolved = []
                    for item in configured:
                        if isinstance(item, dict):
                            name = item.get("token_env", "")
                            if (
                                isinstance(name, str)
                                and name
                                and name not in os.environ
                                and not item.get("token")
                            ):
                                token = file_values.get(name, file_values.get(name.lower()))
                                if token:
                                    item = {**item, "token_env": "", "token": token}
                        resolved.append(item)
                    values = {**values, "knowledge_sources": resolved}
                return values

            return read

        return (
            with_file_tokens(init_settings),
            with_file_tokens(env_settings),
            with_file_tokens(lambda: file_values),
            file_secret_settings,
        )

    @field_validator("knowledge_sources")
    @classmethod
    def unique_knowledge_sources(cls, sources):
        if len({source.id for source in sources}) != len(sources):
            raise ValueError("knowledge source IDs must be unique")
        return sources

    @model_validator(mode="after")
    def reject_retired_knowledge_backends(self):
        if (
            self.knowledge_corpus_path
            or self.portfolio_enabled
            or self.portfolio_scene_catalog_enabled
        ):
            raise ValueError(
                "Local corpus and Portfolio retrieval have been retired; "
                "remove KNOWLEDGE_CORPUS_PATH/PORTFOLIO_* and configure "
                "KNOWLEDGE_SOURCES with knowledge-search-v1"
            )
        return self

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
