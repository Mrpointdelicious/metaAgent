# MetaAgent

基于 LangGraph 的康复交互编排服务。原生入口为 `POST /v1/chat`，提供有界意图解析、能力 DAG、可追溯事实、渐进事件及 PostgreSQL 持久化。当前接入 IREGO 1.6.0 患者工具和场景/医生工具；IREMO、IRETOUR 与医院接口返回未支持。

Dify 按当前约定暂缓，两个 `/compat/dify/...` 入口返回 HTTP 501，`events/dify.py` 保留适配器接口。旧 `orchestration/planner.py`、`workflows/irego.py`、`graph/builder.py` 和旧标签代码保留供历史对照，不由当前容器装配。执行目标和动作是数组，没有五个固定输出槽位。

## 本地运行

```powershell
uv sync --locked --extra dev --extra postgres --extra llm-deepseek
# 仅首次创建配置；已有 .env 时保留原文件。
Copy-Item .env.example .env
uv run python main.py
```

默认 `DRY_RUN=true` 使用明确标记的合成数据，不返回伪造图片或真实场景动作。`PLANNER_MODE=deterministic` 支持明确查询/动作，复杂条件和自由口语会澄清；接真实理解模型需配置 `PLANNER_MODE=llm`、`DEEPSEEK_API_KEY`。`ANSWER_MODE=template` 保持原始数值、单位、缺失状态和来源；`llm_select` 只让模型选择事实编号，失败时回退模板。

`main.py` 使用兼容 Windows PostgreSQL 驱动的 Selector 循环。直接使用 Uvicorn CLI 且在 Windows 接 PostgreSQL 时，添加 `--loop meta_agent.infrastructure.event_loop:loop_factory`。Linux 容器使用默认事件循环。

## 原生请求

可信网关携带服务 Bearer Token，注入真实用户、租户、角色、患者及当前空间。身份不是从用户问题中提取的；服务令牌不能直接分发给患者客户端。

```json
{
  "request_id": "request-unique-001",
  "conversation_id": "conversation-001",
  "user": "trusted-actor",
  "inputs": {"tenantId": "hospital-a", "patientId": "461", "role": "patient"},
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

生产必须配置服务/后端令牌，关闭 `DRY_RUN`，使用 PostgreSQL。v1 只支持单 worker 和单副本，同一数据库的第二实例会因 advisory lock 拒绝启动。实例锁连接失效后停止新操作。运行时不做生产 DDL：

```powershell
docker compose build meta_agent
docker compose up -d postgres
docker compose run --rm migrate
docker compose up -d meta_agent
```

迁移也可直接运行 `uv run python -m meta_agent.infrastructure.migrate`。部署前将 `APP_ENV=production`、`PERSISTENCE_BACKEND=postgres`、`DRY_RUN=false`，并配置可访问的制品主机白名单（后端同主机默认允许）。不要开启多个进程。这里提供部署步骤，本次实现没有替换现有服务。

Checkpoint 只保存运行编号和阶段；完整工具 JSON 存在按作用域隔离的证据库。会话保留最近六轮、闲置 24 小时失效；概览缓存 5 分钟、医生 30 秒；证据和运行默认 30 分钟；匿名运行审计 7 天。到期读拒绝并删除，每小时物理清理，运行删除同时删除 Checkpoint。重启后重复请求返回保守的中断快照，不自动重发动作或制品。持久化原始证据的访问权限和数据库备份应沿用部署方既有管理。

## 知识语料

`KNOWLEDGE_CORPUS_PATH` 指向 JSON 文件，结构见 `docs/implementation-v1/corpus.example.json`。只有 `approved=true` 且填写 `approved_by` 的对应领域分节资料参与检索。使用中文二元片段和英文词的 BM25，一次确定同义替换补检索，返回原文和 `doc_id@version#section_id`，不生成无证据医学结论。文献文件夹中的研究论文不会自动变成已批准的患者知识库。

## 验证

```powershell
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
docker build --target test -t metaagent-test .
docker run --rm metaagent-test
```

PostgreSQL 集成测试需显式设置 `META_AGENT_TEST_POSTGRES_DSN`，仅接受名为 `metaagent_test` 的独立测试库；未提供时该项跳过。实施修订与实际验收结果见 `docs/implementation-v1/实施记录.md`。原冻结文件位于 `docs/freeze-v1.0`，实现修订另行记录。
