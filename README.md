# MetaAgent

## 单 Agent 与三层患者工具（2026-10）

默认 `ORCHESTRATION_MODE=agent` 使用 LangChain `create_agent` 创建的 LangGraph。
一个模型在“回答或调用工具”的循环中自主选择查询、查看失败结果和调整参数重试，
最终直接生成自然语言回答。每轮只自动装填一份可缓存的基本档案，其余查询由模型决定。
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
会话按患者和可信身份隔离，保留最近六轮，另受 `AGENT_HISTORY_TOKENS=8000` 限制，闲置24小时失效。
工具消息关联的证据过期时会标记为需要重新查询，基本档案缓存5分钟。
证据和运行默认30分钟，匿名运行审计7天。到期读拒绝并删除，每小时物理清理，运行删除同时删除 Checkpoint。
重启后重复请求返回保守的中断快照，不自动重发动作或制品。
运行时依赖、令牌和可信身份对象不进入 Checkpoint。

## 知识语料

当前 `search_knowledge` 使用 `KnowledgeAdapter → 本地 approved JSON corpus → BM25`，
仍是简化实现。Dify RAG、独立 RAG Service、Hybrid Retrieval 和 Reranker 均未接入。

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
- Knowledge backend 仍为本地 approved JSON corpus + BM25，尚不是生产级知识服务。
- 自动测试不验收临床诊断或康复建议的医学正确性。
- 受历史上下文预算及证据时效影响，部分结构化训练结果可能在后续轮重新查询。
- Dify 展示已通过原生 API 接入，兼容入口未启用；正式 Dify RAG 留给独立 `feature/knowledge-dify`。

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
