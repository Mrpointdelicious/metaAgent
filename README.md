# MetaAgent

## Dify 空间知识与当前场景（2026-10-22）

应用 `747e56ab-5f99-45ac-b511-a2b960f249c5` 的当前 MetaAgent 容器源码来自
`D:\Project\metaAgent`，分支 `codex/dify-demo`。`D:\Project\metaAgent-dify-demo\metaAgent`
是较早的独立检出。Docker Compose 标签及容器内四个关键文件的 SHA-256 均核对一致。

沿用 main 的 `/v1/chat` 六个顶层字段：`request_id`、`conversation_id`、`user`、`inputs`、
`query`、`response_mode`。Dify 从系统会话和可信环境变量生成请求，保持 `blocking` 模式；
开始节点提供 `spaceId` 下拉输入和 `sceneId` 文本输入。

- `spaceId` 测试值为 `上海中医药大学附属岳阳医院`，决定检索哪家医院的知识。
- `sceneId`（兼容 `scene_id`）表示当前位置，使用 RAG 原文中的房间或场景名称；可省略。
  它独立于 `sceneVersion`，不用于缩小知识检索范围，也不表示已经执行场景切换。
- 启用 `PORTFOLIO_SCENE_CATALOG_ENABLED` 后，场景来自 RAG 当前已发布的结构化目录，
  每 30 秒刷新；来源版本、场景快照和医院知识发布绑定，审核并发布后才生效。
  `domains/space_catalog.json` 仅用于未启用远程目录的离线测试。
  例如 `接待大厅`、`物理治疗诊疗室`、`智能步道康复室1号`、`运动评估室`、`数据中心`。
  原生入口校验当前场景归属；Dify 不再复制静态房间枚举。目录不可用时带场景请求明确失败。
- `META_AGENT__PORTFOLIO_SPACE_KB_IDS` 是可信的 `spaceId → domain → kb_ids` 映射。
  仅 `help` / `product` 使用医院使用说明库，缺失或未知空间不退回其他医院。
  `health` 始终使用全局 `META_AGENT__PORTFOLIO_KB_IDS`，不依赖 `spaceId`。
- 医院使用说明沿用已审核文档和原有患者读取身份，配置于
  `META_AGENT__PORTFOLIO_HELP_TOKEN`；医学文献沿用 `PORTFOLIO_TOKEN` 的既有读取身份。
  患者编号、场景编号及知识库编号不会作为模型可填写的工具参数暴露。
- 空间进入会话隔离键；同院换房间保留会话，换医院隔离历史。`sceneId` 进入请求内容指纹，
  相同 `request_id` 换场景时返回 409。

可审查的 Dify 请求节点源码为 `workflows/dify/prepare_request.py`，图更新器为
`tools/integrate_space_dify.py`。更新时备份原图并检查图哈希，原服务令牌、患者绑定和回答节点沿用。

```powershell
uv run python -m tools.prepare_yueyang_knowledge inspect
uv run python -m tools.prepare_yueyang_knowledge publish
uv run python -m tools.integrate_space_dify update
uv run python -m tools.integrate_space_dify test-draft
uv run python -m tools.integrate_space_dify publish
uv run python -m tools.check_space_runtime
uv run python -m tools.test_space_dify
uv run python -m tools.test_space_dify --context
```

医院资料发布工具只选择已有的最新已审核版本，验证 34 个名称确实出现在原文，
通过现有 RAG 发布、索引校验和激活服务接入检索，不修改文档或审核状态。
备份与联调响应保存于忽略目录 `.local/space-scene/`。

本轮验证：全量 pytest 通过（15 项因 PostgreSQL 测试环境未配置或旧 MaxKB 停用而跳过），
本轮涉及文件的 Ruff 检查通过。已发布 Dify 入口验证了跨房间医院知识查询、当前位置识别、
医学文献来源、同一会话切换房间及省略场景时的默认空间。原生入口拒绝无效/跨医院场景，
未知空间不会退回默认知识库。运行容器内 114 个源码和资源文件与工作目录一致。

## 单 Agent 与三层患者工具（2026-10）

默认 `ORCHESTRATION_MODE=agent` 使用 LangChain `create_agent` 创建的 LangGraph。
一个模型在“回答或调用工具”的循环中自主选择查询、查看失败结果和调整参数重试，
最终直接生成自然语言回答。除普通问候外，每轮自动装填一份可缓存的基本档案，其余查询由模型决定。
中间件管理可信身份、可用工具、历史上下文、证据时效、工具结果裁剪和调用预算，
不再要求模型先生成固定计划，也不使用事实模板拼接患者回答。

三层工具已经在配套 AI_WebApi 中实现并向 Agent 暴露：

- `get_patient_profile`：`project.dbuser.UserDetails` 中的基本信息与档案自述病史，白名单排除姓名和电话。
- `get_patient_consultation`：`dbmedical` 病历、诊断、医嘱、关联处方及 `dbconsultationinfo` 就诊记录，支持分页。
- `get_patient_rehab`：`dbrehaplan` 康复计划，以及 IReTour/IReGo 训练历史。保留各设备的可用状态，区分计划与实际结果。

缺少 IReGo 身份映射或某一设备查询失败时，其他层仍可读取。工具失败作为结果返回模型，
模型可调整查询或回答已有信息；真实失败仍保留在运行记录中。
`AGENT_MAX_MODEL_CALLS=8` 限制模型调用，`MAX_TOOL_CALLS=16` 限制后端调用，
最后一次模型调用只要求形成回答。本地 Knowledge 查询另受模型调用预算和总执行时限约束。
`ORCHESTRATION_MODE=legacy` 可回退到原有 Planner/Compiler/Scheduler 和领域工作流。

已接入 IReTour 的概览、分页历史、单次分析和连续趋势，
以及医院运营查询/报表、可信患者身份映射。IReGo、医生、场景和知识工具保留。
IReTour 历史与 IReGo 一样通过可信患者身份、分页查询、Evidence 和多轮工具上下文接入，
使用独立的 `record_scope=all/with_result/without_result`、项目、训练状态和结果状态筛选。
`IRETOUR_REPORTS_ENABLED=false` 默认关闭 IReTour 单次及趋势报表：模型工具列表不暴露，
Agent、legacy 和底层客户端均禁止调用。报表实现保留，IReGo 报表不受影响。
`MULTISOURCE_PATIENT_CONTEXT_ENABLED=false` 默认禁用多源患者上下文，
同时阻止每轮链首装填、显式患者概况和底层端点调用。旧实现保留，可由配置重新启用。

可信网关输入中的 `patientId` 为 IReGo 的 robotdb 患者编号，
`iretourPatientId` 为 IReTour 的 `project.dbuser.Id`，两个编号不能互相替代。
新 Agent 推荐只传 `projectPatientId`，三层信息和 IReTour 直接使用该身份，
实际查询 IReGo 时才尝试唯一映射。兼容旧的 `iretourPatientId` 输入。
传 `patientPhone` 或使用 legacy 模式时，网关仍先调用 `resolve_patient_identity`，
该路径需要唯一的 robotdb 映射。映射输入不得同时传原始 `patientId`。
患者编号由可信网关注入，不在模型可填写的工具参数中；互相矛盾的 project/IReTour 编号会被拒绝。
医院运营查询需要可信网关注入 `role=operator`；模型只能提供业务查询参数。

IReTour 读接口兼容独立 `iretour-1.0.0` 版本，以及外层 `1.6.0` 且
`meta.registry_versions.contract=iretour-1.0.0` 的早期封套。
趋势分析支持 2–20 次，生成图片支持 3–20 次，可用连续序号或最多180天的日期范围。
“这次/上一次/下一页”保留设备来源；报表失败保留已经获取的事实。
IReTour 单位未知时原样标为未知，数值变化不转换为临床疗效结论。

已有真实测试在独立的本地 AI_WebApi 实例上执行，数据库对照通过只读查询取得。
单 Agent 记录包括10个单轮冒烟用例、12轮连续对话和12次三层接口直连查询。
病史与建议问答只保存原始文本，未评价临床诊断或康复建议的医学正确性。
本地联调脚本、含真实患者信息的 Excel、数据记录和报表原图不进入 Git；
`outputs/` 已由 `.gitignore` 排除。Runtime Cutover 的历史验收记录见
[Runtime Cutover Merge Readiness](docs/runtime-cutover-merge-readiness.md)，最新 Docker 部署验证见本文“验证”。

配套 AI_WebApi 修复包括 IReTour 三个解析依赖注册、project 汇总字段别名和
IReGo 实体的 `robotdb_main` 连接标识。双库配置需启用 `MutiDBEnabled`，
主库连接 project，额外配置 robotdb_main 和 project_identity_temp。
当前本机新版 AI_WebApi 使用独立 Docker 容器和 15043 端口，保留原有 5043 服务。

## Prompt 与出站报文管理

当前 Agent、legacy 意图解析器和事实选择统一通过 `PromptService` 获取提示词。
`prompts/templates/` 保存文本，`prompts/catalog.py` 的 `MANIFEST` 注册 ID、版本、输入类型和片段版本，
`BUNDLES` 将各 Prompt 的版本组合成发布配置。`META_AGENT__PROMPT_BUNDLE=builtin-v1` 为默认配置。
模板随 wheel/Docker 安装包发布，启动时读取并校验；新版本通过新增模板、注册新版本和 bundle 后发布。
已有运行固定其启动时的绑定，配置切换只影响后续运行，不在执行过程中重新读取模板文件。

运行 `metrics` 中的 `prompt_bundle`、`prompt_usages` 记录实际模板版本、内容 hash、片段及模型调用序号，
`agent_tool_sets` 记录模型可见工具集合及其定义 hash。这些新增追踪字段不保存动态 Prompt 正文。
患者基本档案保存在 `RunContext.patient_profile_context`，不再通过 `metrics.patient_profile_context` 返回；
读取旧运行快照时也过滤该旧字段。

业务模块使用 `ctx.events`（`RunEventPublisher`）发布类型化事件，由发布器统一管理回答修订、制品去重和完成事件。
`EventEmitter` 先保存事件再推进可消费序号；SSE 通过 `EventDelivery` 按游标读取并等待变更通知，
blocking 请求无需消费队列。`NativeOutputAdapter` 统一 SSE 编码以及首次 blocking、重复请求、status/cancel 的快照投影。
旧 `/v1/chat` 的 SSE 断线后取消应用和子工具；新 Run API 的订阅独立于运行，断线可回放。
事件保存失败时停止交付并结束等待。当前仍要求单 worker、单副本。

模块职责、版本维护方法及兼容边界见 [Prompt 与出站报文统一管理方案](docs/prompt-and-outbound-management-design.md)。

Prompt 快速切换工具已提供。目标镜像同时包含 `builtin-v1`（当前线上基线）与
`builtin-v2`（仅迭代当前位置更新与房间未知规则），默认仍为 `builtin-v1`。

```powershell
.\tools\switch-prompt.ps1 -List
.\tools\switch-prompt.ps1 -Bundle builtin-v2
.\tools\switch-prompt.ps1 -Bundle builtin-v1
```

脚本从运行容器标签发现 Compose 文件，核验目标镜像内的发布清单，暂停新运行并等待已有运行结束，
只更新运行 env_file 的 `META_AGENT__PROMPT_BUNDLE`，重建 MetaAgent 容器后检查实际版本与 hash。
失败时恢复原配置及镜像；可用版本间切换不构建镜像、不修改 Dify 工作流。
完整操作及新增版本步骤见 [Prompt 部署与回滚](docs/prompt-deployment.md)。

## 前端 Run API v0.1

新增 `POST /v1/agent/runs`，立即返回 `202 + run_id`；前端通过独立的
`GET /v1/agent/runs/{run_id}/events` 获取八种结构化事件。SSE `id` 使用 `seq`，
支持 `after_seq` / `Last-Event-ID` 接续历史与实时事件。断开订阅不会取消运行，
取消使用 `POST /v1/agent/runs/{run_id}/cancel`，状态使用 `GET /v1/agent/runs/{run_id}`。
动作执行结果通过 `POST /v1/agent/runs/{run_id}/actions/{action_id}/ack` 确认，
与服务端传输状态分别持久化。原 `/v1/chat` 及其状态、取消接口保持兼容。

调用方继续使用服务 Bearer 认证；可信网关注入 `X-End-User-ID`、`X-Tenant-ID` 和角色，
并校验患者授权。新接口 `context.patient_id` 统一为 `project.dbuser.Id`。
UI 选择的记录先在该患者的历史中确认，再作为会话锚点；切换患者的会话状态独立。
接口不返回内部 metrics、计划或工具结果。请求/上下文与响应字段已加入 OpenAPI。

请求示例、错误契约、重连/ACK 规则及本阶段边界见 [前端通信接口 v0.1](docs/agent-run-api-v0.1.md)。

## Dify 展示入口

当前本机 Dify 已接入统一的“康复与就诊助手”，通过原生 `POST /v1/chat` 调用 MetaAgent，
使用 `response_mode=blocking`。Chatflow 为：开始 → 准备可信请求 → HTTP 调用 → 整理回答 → 展示。
患者绑定、租户、角色和空间信息由服务端配置注入；服务 Bearer Token 保存在 Dify secret 环境变量中。
工具选择、模型调用和连续对话由 MetaAgent 的单 Agent 运行时完成。

Dify 的 `METAAGENT_URL` 配置为 `http://meta_agent:8080`，通过共享 Docker 网络访问服务。
页面先展示最终回答，中间步骤统一附在回答末尾，不展示原始工具 JSON；
`show_workflow_steps=false`、`prompt_public=false`。迁移保留了原应用、展示链接和患者绑定。
Dify RAG 尚未接入。两个 `/compat/dify/...` 入口仍返回 HTTP 501，`events/dify.py` 保留适配器接口。

旧 `orchestration/planner.py`、`workflows/irego.py`、`graph/builder.py` 和旧标签代码保留供历史对照，
不由当前容器装配。执行目标和动作是数组，没有五个固定输出槽位。

## legacy 模式的 IReGo 工作流

Planner 只决定业务目标（`IReGoRequest`：operation/selector/topics/need_artifact/force_refresh），
不规划 IReGo 内部工具步骤：LLM 无权生成 `after_goal_ids`/`condition` 依赖边，Compiler 不再把模型声明的依赖提升为执行约束。
Scheduler 只看到一个 `irego.execute` 任务，内部固定链由 `domains/irego.py` 决定：

- `overview`：患者概览 → Evidence → Fact/FactView（带缓存）
- `history`：训练历史 → Evidence → Fact/FactView
- `session`：定位 session_ref（复用新鲜锚点/最新可用/历史发现）→ 会话分析 → Evidence → Fact/FactView → 更新记录锚点 → `need_artifact=true` 时以同一 session_ref 生成报告
- `trend`：连续窗口趋势 → Evidence → Fact/FactView → `need_artifact=true` 时生成趋势报告

只要进入 IReGo 就必然执行对应 operation 的基础数据查询并建立事实层（"LLM 不需要读"不等于"不获取"）；
报告只是可选制品，失败不会抹掉已建立的事实（目标降级为 partial）。
回答由 Context Selector 从事实层选取，模型只选择事实编号。报告只由 `need_artifact` 控制，绝不因用户只索取解释而生成。

## 宿主机开发运行（可选）

当前本机演示服务使用 Docker，宿主机旧服务已停用。以下命令供单独开发调试使用：

```powershell
uv sync --locked --extra dev --extra postgres --extra llm-deepseek
# 仅首次创建配置；已有 .env 时保留原文件。
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
uv run python main.py
```

默认 `DRY_RUN=true` 使用明确标记的合成工具数据，不返回伪造图片或真实场景动作。
单 Agent 仍需真实模型，配置 `DEEPSEEK_API_KEY`，`AGENT_MODEL` 留空时使用 `PLANNER_MODEL`。
接真实 AI_WebApi 时设置 `DRY_RUN=false` 和后端地址/令牌，并部署配套三层工具。
`PLANNER_MODE`、`ANSWER_MODE` 只影响 legacy 模式；`deterministic` 使用确定性目标解析，
`template`/`llm_select` 使用原来的事实回答模块。

`main.py` 使用兼容 Windows PostgreSQL 驱动的 Selector 循环。直接使用 Uvicorn CLI 且在 Windows 接 PostgreSQL 时，添加 `--loop meta_agent.infrastructure.event_loop:loop_factory`。Linux 容器使用默认事件循环。

## 原生请求

可信网关携带服务 Bearer Token，注入真实用户、租户、角色、患者及当前空间。身份不是从用户问题中提取的；服务令牌不能直接分发给患者客户端。

```json
{
  "request_id": "request-unique-001",
  "conversation_id": "conversation-001",
  "user": "trusted-actor",
  "inputs": {"tenantId": "hospital-a", "projectPatientId": "3799", "role": "patient"},
  "query": "解读最近训练并生成报告图片",
  "response_mode": "streaming"
}
```

`conversation_id` 可省略，首次请求返回生成值，续聊必须复用它。`request_id` 在同一作用域内标识一次请求；相同编号、相同内容返回运行快照，内容不同返回 409。在去重保留期内不重新发动作。流式重复请求返回 JSON 快照（运行中为 202），不是第二条 SSE 订阅。

- `POST /v1/chat`：`streaming` 为 SSE，`blocking` 为终态 JSON。
- `POST /v1/runs/{run_id}/status`、`POST /v1/runs/{run_id}/cancel`：请求体为相同的 `user`、`inputs`；不同作用域返回 404。
- `GET /health/live`、`/health/ready`、`/health/dependencies`：分别检查进程、持久化与实例锁、外部服务。

SSE 的 `data` 为 `OutboundEvent`：`accepted → progress → action_ready / answer_part / artifact_ready / clarification / task_failed → completed`。事件带单调 `seq`、运行/目标/任务编号。一个目标的新 `answer_part` 用 `revision` 和 `replaces` 替换旧解释，客户端应按目标更新，而非重复播报全部旧内容。`completed` 包含每个目标/任务的真实终态。

场景动作需显式启用 `SCENE_ACTIONS_ENABLED=true`，并提供 `spaceId`、`sceneVersion`。指令必须来自后端当前目录、逐项对应原话；仅接受匹配项，保留重复和顺序。切换空间后停止旧目录的后续动作。`dispatched` 仅代表交给输出通道，不证明 Unity 执行完成；阻塞返回和断线保留 `delivery_unknown`。状态与去重快照不会重放 `action_ready`。医生查询只返回数量和姓名，不自动联系医生。

## 持久化与部署

生产必须配置服务/后端令牌，关闭 `DRY_RUN`，使用 PostgreSQL。v1 只支持单 worker 和单副本，同一数据库的第二实例会因 advisory lock 拒绝启动。实例锁连接失效后停止新操作。部署时显式执行迁移，运行时不做生产 DDL。

### 当前本机 Docker 部署（2026-10-08）

Compose 项目名为 `metaagent-runtime`，Docker Desktop 在该项目下统一显示以下独立容器：

| Compose 服务 | 容器名 | 职责 | 宿主机端口 |
| --- | --- | --- | --- |
| `meta_agent` | `meta_agent` | 单 Agent、工具编排、对话和上下文 | `5080 → 8080` |
| `ai_webapi_runtime` | `meta_agent_webapi` | 新版 AI_WebApi、患者和训练查询、报表生成 | `15043 → 8080` |
| `postgres` | `metaagent-runtime-postgres-1` | 运行记录、Evidence、会话及 Checkpoint 持久化 | 不发布；Docker 内部 `5432` |

`metaagent-runtime` 是 Compose 项目分组；“单 Agent”描述模型编排方式，配套业务接口和数据库各自运行。
PostgreSQL 的容器名按“项目名、服务名、实例序号”生成。三个服务可统一管理，也可分别查看日志、更新和重启；
启动时等待依赖健康，容器通过服务名通信。

MetaAgent 通过 `http://ai_webapi_runtime:8080` 访问新版后端，通过 `postgres:5432` 访问独立数据库。
该 PostgreSQL 与 Dify 的数据库分开管理，不占用 Dify 数据库。
数据库使用命名卷 `metaagent-runtime_meta_agent_postgres_data`；患者报表和医院报表也分别使用命名卷，
重建应用容器时保留数据。删除数据卷会删除其中的数据，日常停启保留这些卷。

当前部署在仓库 `compose.yaml` 上叠加本机私有配置：

- `outputs/20261008-docker-cutover/compose.override.yaml`：新版 AI_WebApi、报表卷及外部网络。
- 同目录的 `compose.env`、`metaagent.env`、`backend.env`：Compose、MetaAgent 和后端的本机配置。
- `start-docker.ps1`：启动依赖、显式运行数据库迁移，再启动 MetaAgent。

这些配置和测试资产位于已忽略的 `outputs/` 下，不进入 Git；新检出仓库需另行准备配置。
在当前本机、仓库根目录执行：

```powershell
& ./outputs/20261008-docker-cutover/start-docker.ps1

# 后续管理必须使用相同项目名和配置文件。
$runtimeComposeArgs = @(
    'compose', '--project-name', 'metaagent-runtime',
    '--env-file', 'outputs/20261008-docker-cutover/compose.env',
    '-f', 'compose.yaml',
    '-f', 'outputs/20261008-docker-cutover/compose.override.yaml'
)
docker @runtimeComposeArgs ps
docker @runtimeComposeArgs logs --tail 100 meta_agent
docker @runtimeComposeArgs logs --tail 100 postgres
```

`GET http://localhost:5080/health/ready` 检查持久化和实例锁，
`GET http://localhost:5080/health/dependencies` 检查 AI_WebApi。
当前 MetaAgent、AI_WebApi 和 PostgreSQL 均配置为 `restart: unless-stopped`；
旧演示启动脚本已改为调用 Docker 启动脚本，宿主机不再启动对应 Python/.NET 服务。

MetaAgent 与 Dify API 使用共享网络 `docker_default`，新版 AI_WebApi 与既有 MySQL 使用 `mysql_default`。
这两个外部网络需已存在；移植部署时应按目标环境调整。
报表 `PublicBaseUrl` 需同时能被 MetaAgent 容器和展示浏览器访问，并加入 `ARTIFACT_ALLOWED_HOSTS`。
宿主机地址变更时需同步本机后端配置及白名单。

### 新环境的基础 Compose 部署

仓库内的 `compose.yaml` 提供 MetaAgent、PostgreSQL 和一次性 `migrate` 服务；新版 AI_WebApi 及 Dify 网络由部署覆盖文件补充。
`.env.example` 的默认值用于开发，不会自动启用生产 PostgreSQL 持久化。先准备 `.env`，至少设置：

- `META_AGENT__APP_ENV=production`、`META_AGENT__DRY_RUN=false`。
- `META_AGENT__PERSISTENCE_BACKEND=postgres`、`META_AGENT__AUTO_SETUP_PERSISTENCE=false`。
- `META_AGENT_POSTGRES_PASSWORD`，以及使用相同密码、主机名 `postgres`、数据库名 `meta_agent` 的 `META_AGENT__POSTGRES_DSN`。
- 服务与 AI_WebApi Bearer Token、模型 API Key、后端地址和制品主机白名单。
- `META_AGENT__WORKER_COUNT=1`、`META_AGENT__PLANNER_MAX_RETRIES=0`。

完成配置后，在新环境显式选择项目名并执行：

```powershell
docker compose --project-name metaagent-runtime build meta_agent
docker compose --project-name metaagent-runtime up -d --wait postgres
docker compose --project-name metaagent-runtime --profile maintenance run --rm migrate
docker compose --project-name metaagent-runtime up -d --wait meta_agent
```

基础 Compose 命令不包含当前本机的覆盖配置；管理当前已运行的服务使用上一节的命令。
迁移也可直接运行 `uv run python -m meta_agent.infrastructure.migrate`，需使用与目标实例一致且可访问数据库的配置。

### 持久化行为

Agent Checkpoint 保存该次运行的模型消息和裁剪后的工具消息；完整工具 JSON 存在按作用域隔离的证据库。
会话按患者和可信身份隔离，保留最近六轮，另受 `AGENT_HISTORY_TOKENS=24000` 限制，闲置24小时失效。
单项工具结果默认预算为 `AGENT_TOOL_OUTPUT_TOKENS=16000`，模型上下文保护上限为
`MODEL_CONTEXT_TOKENS=65536`；在预算内保留完整工具结果，超出后才逐级裁剪并标记。
工具消息关联的证据过期时会标记为需要重新查询，基本档案缓存5分钟。
证据和运行默认30分钟，匿名运行审计7天。到期读拒绝并删除，每小时物理清理，运行删除同时删除 Checkpoint。
重启后重复请求返回保守的中断快照，不自动重发动作或制品。
运行时依赖、令牌和可信身份对象不进入 Checkpoint。

## 知识语料

2026-10-23 医疗知识已从 8 份扩充至 17 份：新增 6 篇用户已审核正文、2 份 NICE 指南和 WHO 神经康复干预包 Module 3 正文。
医疗文献保持全局检索，不绑定 spaceId；WHO 正文按用户确认的当前非商业用途发布。
版次、发布记录与质量限制见 [医疗知识发布记录](docs/medical-publication-20261023.md)。

当前已发布 Dify 应用通过原生 `/v1/chat` 调用 MetaAgent，health 文献检索使用
`KnowledgeAdapter → rag-portfolio API`，由独立 RAG 服务完成混合召回、PostgreSQL
资格核验、重排及证据组装。医院说明库（help/product）按 `spaceId` 选择，
医学文献（health）全局检索；未启用 portfolio 的部署仍可配置本地 approved JSON corpus。

2026-10-23 新增 `builtin-v3` Prompt：每轮最多 4 个检索子问题、同领域最多 2 次调用、
总计 4000 上下文 token。RAG 按知识库及固定重排模型的校准阈值拒绝低相关性资料；
重排故障不使用融合分数回退。检索记录保留策略版本、分数和子问题覆盖情况。

旧 MaxKB 在当前运行链路明确禁用，不参与检索、回答或故障回退。Compose 固定
`META_AGENT__MAXKB_ENABLED=false`；即使历史配置传入 true，也不会装配 MaxKB 工作流。
旧 `/v1/knowledge/retrieve` 和 `/v1/knowledge/sources/{code}` 接口保留鉴权，
对合法的已认证请求返回 `503 / maxkb_disabled`。`/compat/dify/...` 仍返回 501。

`KNOWLEDGE_CORPUS_PATH` 指向本地 JSON。格式由
[domains/knowledge.py](src/meta_agent/domains/knowledge.py) 中的
`Corpus`、`Document`、`Section` 定义：顶层 `schema_version="1.0"` 和 `documents`，
文档包含领域、版本、来源、审批信息及分节文本。
只有 `approved=true` 且填写 `approved_by` 的对应领域资料参与检索。
使用中文二元片段和英文词的 BM25，并在无结果时执行一次确定性同义替换补检索，
返回原文和 `doc_id@version#section_id`。语料不可用或没有匹配时返回 unavailable。
文献文件夹中的研究论文不会自动成为已批准的患者知识库。

## 已知限制

- 当前只支持单 worker、单副本；跨实例租约与事件回放不在 v1 范围内。
- 当前配置了岳阳医院一个空间；其他医院需先配置知识库映射和经来源核对的场景目录。
- 自动测试不验收临床诊断或康复建议的医学正确性。
- 受历史上下文预算及证据时效影响，部分结构化训练结果可能在后续轮重新查询。
- Dify 展示已通过原生 API 接入，旧 Dify 兼容入口与旧 MaxKB 链路未启用。

详见 [Runtime Cutover Known Issues](docs/runtime-cutover-known-issues.md)。

## 验证

```powershell
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
docker build --target test -t metaagent-test .
docker run --rm metaagent-test
```

PostgreSQL 集成测试需显式设置 `META_AGENT_TEST_POSTGRES_DSN`，仅接受名为 `metaagent_test` 的独立测试库。
未提供时属于 environment-dependent skip，不视为 failure。Docker 内运行该测试时还需连接测试数据库所在网络。

`tests/test_maxkb.py` 中 14 项历史运行链路用例明确跳过，不计为功能通过；其中包含先前失败的
12 项及此前因同样返回 503 而碰巧通过的 2 项。独立的目录完整性和准备工具测试继续运行。
`tests/test_maxkb_disabled.py` 验证旧接口的鉴权及禁用状态，包括历史开关为 true 的情况。

2026-10-08 本机 Docker 迁移验证结果：

- 完整 Python 回归：152 passed / 0 failed / 0 skipped，包含独立 `metaagent_test` 数据库的 PostgreSQL 集成测试。
- `ruff check src tests` 和 `ruff format --check src tests` 均通过。
- 容器内三层患者直连查询及 IReTour 历史查询均成功；多源接口和两项 IReTour 报表调用保持禁用。
- 已发布 Dify WebApp 完成 1 次单轮冒烟和 2 次连续追问：2 succeeded / 1 partial / 0 hard failures。
  `partial` 来自该演示患者没有 IReTour 可用结果，IReGo 分析和原始报表仍成功生成。
- 重启 MetaAgent 后，同一运行的记录保持一致，后续连续追问成功。
- 结果保存在本机 Excel，原始 PNG 按字节保留并嵌入工作簿；仅检查文本、HTTP 和文件字节，未做视觉复核或医学正确性评价。

测试资产位于已忽略的 `outputs/20261008-docker-cutover/`。Runtime Cutover 的历史收口验证见
[Runtime Cutover Merge Readiness](docs/runtime-cutover-merge-readiness.md)。

## 多知识源接入（首选）

`META_AGENT__KNOWLEDGE_SOURCES` 配置多个独立知识服务，每项包含逻辑 `id`、`description`、
完整 `url`、支持的 `domains`、可选 `scoped_domains` 和只读凭据。同领域可以接入多个知识源。
`search_knowledge` 的模型参数为 `query/domain/source`；省略 `source` 时选配置中第一个支持该领域的
知识源。模型只能看到逻辑名称、用途和领域，看不到地址、凭据、患者身份或内部 KB ID。

```json
[
  {"id":"medical","description":"医学文献","url":"http://rag-portfolio-api:8000/v1/knowledge/search","domains":["health"],"token_env":"MEDICAL_KNOWLEDGE_TOKEN"},
  {"id":"hospital","description":"医院说明","url":"http://rag-portfolio-api:8000/v1/knowledge/search","domains":["help","product"],"scoped_domains":["help","product"],"token_env":"HOSPITAL_KNOWLEDGE_TOKEN"}
]
```

环境变量中的配置需写成一行 JSON。`token_env` 引用进程环境变量（Compose 的 `env_file` 会注入）；
本地启动可用 `uv run --env-file .env ...` 注入，或在各项中直接配置受 `SecretStr` 保护的 `token`。
`scoped_domains` 对应的 `scope.space_id` 仅由可信运行上下文注入，知识服务仍须独立鉴权。

各服务实现固定的 `knowledge-search-v1` 协议：请求携带问题、领域、可信作用域与单次上下文上限；
响应为 `status/context/sources/gaps/trace_id`。Agent 使用打包的 OpenAPI 契约校验收发数据，
统一执行超时、响应大小限制、整轮工具次数及并发控制、证据存储和引用传递。
`KNOWLEDGE_CONTEXT_TOKENS` 为**每次调用**上限，整轮资源限制继续由 Agent 总工具预算与上下文预算控制；
新协议不沿用旧 portfolio 的每领域次数/子问题计数。

检索策略、KB 映射、版本资格和重排归服务端。新增符合协议的知识源只需添加配置；
原生 API 格式不同的服务需在其服务侧转换为该协议。调用失败、权限拒绝或无证据均不会自动换源，
也不会回退本地 corpus。来源保留在证据与 `knowledge_retrieval` 指标中，Dify 继续展示引用；跨轮重新检索。

契约源位于 rag-portfolio。更新后执行：

```powershell
# 在 rag-portfolio 仓库执行；同时导出服务契约与 Agent 消费快照。
uv run python tools/export_knowledge_contract.py --agent-root D:/Project/metaAgent
```

`KNOWLEDGE_SOURCES` 非空时优先使用该接入方式。留空则沿用以下 portfolio / 本地 corpus 兼容路径；
场景目录与导航仍使用独立接口。

新契约部署使用 `META_AGENT__PROMPT_BUNDLE=builtin-v4`，其知识检索规则对应 `query/domain/source`
及证据缺口语义。历史 `builtin-v1/v2/v3` 的模板与发布 hash 保持冻结，可用于旧接入回滚。

## Dify 与现有 RAG 知识库（兼容配置）

应用 `747e56ab-5f99-45ac-b511-a2b960f249c5` 已接入现有 rag-portfolio，沿用五节点的
`准备请求 → MetaAgent → 回答展示` 工作流。MetaAgent 的 `search_knowledge` 只传问题和配置的 KB ID，
由 RAG API 完成 Hybrid、PG 资格核验、重排和证据打包。返回的标题、物理页码、版本、chunk hash
保存在证据 Store，并由 Dify 确定性展示“本次检索资料”。跨轮知识结果会标记过期，要求重新检索当前发布。

配置见 `.env.example` 中的 `META_AGENT__PORTFOLIO_*`。首次接入发布 health 文献，
2026-10-22 增加按 `spaceId` 选择的医院说明库及 help/product 工具领域。
RAG 凭据为范围受限的只读身份；查询请求不附带患者编号。
已有本地语料路径保留；旧 MaxKB 配置仅供历史实验工具读取，当前链路禁用。
RAG 服务失败会明确返回不可用，不回退到 MaxKB 或本地语料。

兼容构建入口 `Dockerfile.portfolio` 基于原运行镜像更新锁定依赖和应用，包含新契约校验依赖。
Compose 的实际 image 选择和服务配置仍由已有 `outputs/20261020-docker-cutover/` 管理。
rag-portfolio 的 `compose.integration.yaml` 将 API 接入现有 MetaAgent 网络，并挂载已有 `data/`；
数据库地址使用完整容器名，避免两张网络都有 `postgres` 服务名导致串库。

```powershell
uv run python tools/integrate_portfolio_dify.py update
uv run python tools/integrate_portfolio_dify.py test-draft
uv run python tools/integrate_portfolio_dify.py publish
uv run python tools/test_portfolio_dify.py --fault
```

前两步使用 Dify 自身的工作流服务保存草稿、执行真实 Sandbox/HTTP 节点。发布保留 Dify 版本历史。
最终测试使用已发布 WebApp 的正常 passport/聊天接口，验证中文文献、跨轮新 trace、英文来源及故障提示。
`--fault` 会短暂停止新 RAG API，并在 finally 中恢复。草稿备份和测试记录只保存在忽略目录
`.local/portfolio-dify/`，不包含持久凭据。此验证证明联调与来源展示有效，不代表医学答案正确性验收。
