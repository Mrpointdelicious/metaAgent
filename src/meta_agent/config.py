"""
创建日期：2026-08-29
文件功能：定义环境变量配置、生产运行门和外部依赖参数。
"""

import os
from functools import lru_cache
from typing import Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from meta_agent.integrations.knowledge import KnowledgeSourceConfig
from meta_agent.roles import Role

# 已注册的运行分支；客户端档案只能绑定到这些分支，新增分支时同步这里与 flows 注册表。
# ai_* 五个分支是功能令牌的独立完整流程（见 flows/fixed_task.py）。
SUPPORTED_CLIENT_FLOWS = frozenset(
    {
        "default",
        "consultation_room",
        "ai_summary",
        "ai_medical_order",
        "ai_prescription",
        "ai_realtime_analysis",
        "ai_consultation",
    }
)


class ClientTokenConfig(BaseModel):
    """一个接入方的功能档案：令牌决定可见工具、独立流程分支和专属提示词。

    令牌本身只用于识别“是谁在调用”，功能差异由 flow（绑定的独立流程分支）、
    工具白名单和 prompt_suffix 表达，避免为每个 app 复制一套认证代码。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    token: SecretStr
    # 空表示沿用 pos 判定（default / consultation_room）；非空则强制该独立流程分支。
    flow: str = Field(default="", max_length=32)
    # allowed_tools 非空即白名单；与 excluded_tools 只能二选一，避免语义冲突。
    allowed_tools: list[str] = Field(default_factory=list, max_length=64)
    excluded_tools: list[str] = Field(default_factory=list, max_length=64)
    # 追加在系统提示词末尾的功能边界说明；不能覆盖基础安全约束。
    prompt_suffix: str = Field(default="", max_length=4000)

    @field_validator("token")
    @classmethod
    def nonempty_token(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        if len(raw) < 8:
            raise ValueError("客户端令牌不能为空且长度至少 8 个字符")
        if raw != value.get_secret_value():
            raise ValueError("客户端令牌不能包含首尾空格")
        return value

    @field_validator("flow")
    @classmethod
    def valid_flow(cls, value: str) -> str:
        value = value.strip()
        if value and not value.replace("_", "").isalnum():
            raise ValueError("flow 必须是分支标识（字母、数字、下划线）")
        return value

    @field_validator("allowed_tools", "excluded_tools")
    @classmethod
    def unique_tool_names(cls, values: list[str]) -> list[str]:
        cleaned = [item.strip() for item in values if item and item.strip()]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("工具名不能重复")
        return cleaned

    @model_validator(mode="after")
    def allowed_and_excluded_are_exclusive(self):
        if self.allowed_tools and self.excluded_tools:
            raise ValueError("allowed_tools 与 excluded_tools 只能配置一个")
        return self


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
    service_bearer_token_operator: SecretStr = SecretStr("")
    disabled_agent_roles: frozenset[Role] = frozenset()
    explicit_role_clients: frozenset[Literal["primary", "secondary"]] = frozenset(
        {"primary", "secondary"}
    )
    public_scene_controls: dict[str, dict[int, frozenset[str]]] = Field(default_factory=dict)
    resource_authorization_url: AnyHttpUrl | None = None
    resource_authorization_token: SecretStr = SecretStr("")
    # secondary 客户端禁用的工具名集合（默认去掉挂号 search_doctors）。
    secondary_excluded_tools: list[str] = Field(default_factory=lambda: ["search_doctors"])
    # 多接入方令牌表：每个条目把一个 Bearer Token 映射到独立客户端档案与独立流程分支。
    # 配置为 JSON 数组，例如
    # [{"name":"summary","token":"app-...","flow":"ai_summary"}]
    service_clients: list[ClientTokenConfig] = Field(default_factory=list, max_length=32)
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
    # 长期记忆仅保存非医疗枚举偏好；启用前执行显式数据库迁移。
    patient_memory_enabled: bool = False
    patient_memory_timeout_seconds: float = Field(default=3.0, ge=0.1, le=30)

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
    # AI问诊意图识别：默认用 LLM 做语义分类，比纯关键词规则更准（过渡语/省略句）。
    # 场景跳转仍由本地白名单硬前置，避免 LLM 编造点位；模型不可用时自动降级到规则。
    consultation_intent_llm: bool = True
    # AI问诊分诊是否检索 health 域（clinical_kb+rehab_kb）医学知识，结合患者档案作答。
    consultation_medical_kb: bool = True
    scene_enabled: bool = True
    doctors_enabled: bool = True
    scene_actions_enabled: bool = False
    # 场景指令改为 agent 本地解析（直连 webyun 点位库），不再调用 AI_WebApi 的
    # navigate_scene 端点。设为 false 可回滚到旧的 .NET 匹配器。
    scene_local_enabled: bool = True
    scene_backend_version_url: str = "http://192.168.8.160/back/version"
    scene_backend_query_url: str = "http://192.168.8.160/back/query"
    scene_backend_timeout_seconds: float = Field(default=15, gt=0, le=60)
    scene_default_space_id: str = "67ecd511-5532-405d-9b70-1cd1c0532eed"
    # 必填：场景上游 192.168.8.160/back/* 的 apikey / apitoken（代码内不保留默认值）。
    scene_backend_api_key: str = ""
    scene_backend_api_token: SecretStr = SecretStr("")
    knowledge_corpus_path: str = ""
    knowledge_access_revision: str = Field(default="1", min_length=1, max_length=128)
    # 多知识源统一契约（knowledge-search-v1）；非空时 search_knowledge 优先走此接入。
    knowledge_sources: list[KnowledgeSourceConfig] = Field(default_factory=list, max_length=20)
    # 兼容旧配置名；知识检索统一使用 search_knowledge，不再启用独立企业问答。
    knowledge_backend: Literal["maxkb"] = "maxkb"
    knowledge_context_tokens: int = Field(default=4000, ge=128, le=16000)
    # rag-portfolio 接入：health 全局文献 + 按 spaceId 选择的医院 help/product。
    portfolio_enabled: bool = False
    portfolio_roles: frozenset[Role] = frozenset()
    portfolio_base_url: str = "http://rag-portfolio-api:8000"
    portfolio_token: SecretStr = SecretStr("")
    portfolio_kb_ids: list[str] = Field(default_factory=list)
    portfolio_space_kb_ids: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    portfolio_help_token: SecretStr = SecretStr("")
    portfolio_timeout_seconds: float = Field(default=20, gt=0, le=40)
    portfolio_top_k: int = Field(default=5, ge=1, le=20)
    portfolio_context_tokens: int = Field(default=4000, ge=128, le=16000)
    portfolio_scene_catalog_enabled: bool = False
    portfolio_scene_catalog_ttl_seconds: float = Field(default=30, ge=1, le=300)
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
    # AI推荐处方直连的处方接口基址（Dify 应用用的是公网网关，内网部署用 8085）。
    prescription_api_base: str = "http://192.168.8.71:8085"
    # 处方缺 Type 时是否允许流程自动新建（对生产库有写入副作用，默认关闭）。
    prescription_auto_create: bool = False
    # 在线医生改为 agent 本地查询（直连 webyun 在线医生列表），不再调用 AI_WebApi 的
    # search_doctors 端点。设为 false 可回滚到 .NET 实现。
    doctors_local_enabled: bool = True
    # 仅已主动启用旧共享策略的接入允许患者、医生查询运营数据。
    hospital_ops_open_to_all_roles: bool = False
    hospital_directory_public: bool = False
    doctor_backend_login_url: str = "http://www.ga-robot.com/webyun/api/Login/Login"
    # 房间外挂号（search_doctors）用 /api/users（在线接诊医生）；
    # 房间内邀请医生走 /api/users/all（在房间中的医生），两者口径不同，不要混用。
    doctor_backend_query_url: str = "http://192.168.8.71:4000/api/users"
    doctor_backend_timeout_seconds: float = Field(default=15, gt=0, le=60)
    doctor_token_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    doctor_default_space_id: str = ""
    # 医生专用账号；留空时沿用 webyun_login_phone / webyun_login_password。
    doctor_login_phone: str = ""
    doctor_login_password: SecretStr = SecretStr("")

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
                            native = item.get("maxkb")
                            if isinstance(native, dict):
                                native = dict(native)
                                for field in ("username", "password"):
                                    name = native.get(field + "_env", "")
                                    if name and name not in os.environ and not native.get(field):
                                        secret = file_values.get(
                                            name, file_values.get(name.lower())
                                        )
                                        if secret:
                                            native.update({field + "_env": "", field: secret})
                                item = {**item, "maxkb": native}
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
    def validate_client_registry(self):
        """令牌表内部唯一，且不能与旧 primary/secondary 冲突。"""
        names = [client.name for client in self.service_clients]
        if len(set(names)) != len(names):
            raise ValueError("service_clients 的 name 必须唯一")
        tokens = [client.token.get_secret_value() for client in self.service_clients]
        if len(set(tokens)) != len(tokens):
            raise ValueError("service_clients 的 token 必须唯一")
        legacy = {
            "primary": self.service_bearer_token,
            "secondary": self.service_bearer_token_secondary,
        }
        if "backoffice" in names:
            raise ValueError("backoffice 是后台运营专用接入方")
        legacy["backoffice"] = self.service_bearer_token_operator.get_secret_value()
        for name in names:
            if legacy.get(name):
                raise ValueError(f"service_clients 不能重复使用已配置的 {name} 名称")
        configured_legacy = {token for token in legacy.values() if token}
        if len(configured_legacy) != sum(bool(token) for token in legacy.values()):
            raise ValueError("各接入方必须使用独立的令牌")
        if configured_legacy.intersection(tokens):
            raise ValueError("service_clients 的 token 不能与旧令牌重复")
        return self

    def runtime_issues(self) -> list[str]:
        issues = []
        operator_token = self.service_bearer_token_operator.get_secret_value()
        if operator_token and operator_token in (
            self.service_bearer_token,
            self.service_bearer_token_secondary,
        ):
            issues.append("运营后台必须使用与前台不同的服务凭证")
        url = self.resource_authorization_url
        if bool(url) != bool(self.resource_authorization_token):
            issues.append("业务授权校验地址和凭证必须同时配置")
        if url and (
            url.scheme != "https" or url.username or url.password or url.query or url.fragment
        ):
            issues.append("业务授权校验地址必须是无凭证、查询参数和片段的 HTTPS 地址")
        if self.worker_count != 1 or os.environ.get("WEB_CONCURRENCY", "1") != "1":
            issues.append("v1仅支持一个worker；扩容须实现跨进程租约与事件回放")
        for client in self.service_clients:
            if client.flow and client.flow not in SUPPORTED_CLIENT_FLOWS:
                issues.append(
                    f"service_clients.{client.name} 的 flow={client.flow!r} 未注册；"
                    f"可选 {sorted(SUPPORTED_CLIENT_FLOWS)}"
                )
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
