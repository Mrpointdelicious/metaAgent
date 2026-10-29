# 工具与轻量知识库迁移

基线：`origin/master` 的 `0620db1`，保留 `7249dce` 和 `0620db1` 的新增代码。
工具和统一知识接口取自 `codex/dify-demo` 的 `c253d3e`，按依赖迁移。

## 患者工具

原有 18 个 Agent 工具保留，新增 IReCog 和线上量表各 6 个：概览、历史、单次分析、纵向分析、单次报表、纵向报表。
IReCog 使用 `irecog-1.0.0` 契约，量表使用 `assessment-1.0.0` 契约。
可信 project 患者编号由服务注入，模型不能指定患者身份；IReGo 的 robot 身份映射保持原逻辑。
两类工具均接入 Agent 和 legacy 工作流，保留记录来源及跨轮设备边界。

读接口直接复用现有 `META_AGENT__AI_WEBAPI_BASE_URL` 和后端凭据。
只有报表生成需要显式开启以下配置，默认关闭：

```dotenv
META_AGENT__IRECOG_REPORTS_ENABLED=false
META_AGENT__ASSESSMENT_REPORTS_ENABLED=false
```

量表报表先重读同参数分析，再把分析的 `source_snapshot` 作为 `expected_source_snapshot` 交给报表端点。
来源快照缺失时停止生成。IReCog 的单次结果按 `data.sessions` 读取和校验，修复指定记录误报不匹配的问题。

## 在 .env 配置知识源

需要直接使用现有 MaxKB 时，可配置 `protocol=maxkb`，详见
[MaxKB 原生 API 直连](maxkb-direct.md)。该模式在 Agent 内直接调用 MaxKB，不需要额外桥接容器。

将以下配置加入部署使用的 `.env`，替换真实地址和凭据；`KNOWLEDGE_SOURCES` 的 JSON 必须写在一行：

```dotenv
META_AGENT__KNOWLEDGE_SOURCES=[{"id":"medical","description":"医学文献","url":"http://rag-portfolio-api:8000/v1/knowledge/search","domains":["health"],"token_env":"MEDICAL_KNOWLEDGE_TOKEN"},{"id":"hospital","description":"医院说明与产品资料","url":"http://rag-portfolio-api:8000/v1/knowledge/search","domains":["help","product"],"scoped_domains":["help","product"],"token_env":"HOSPITAL_KNOWLEDGE_TOKEN"}]
META_AGENT__KNOWLEDGE_CONTEXT_TOKENS=4000
MEDICAL_KNOWLEDGE_TOKEN=replace-with-medical-service-token
HOSPITAL_KNOWLEDGE_TOKEN=replace-with-hospital-service-token
```

支持一个或多个独立服务，也支持同一地址配置不同凭据与领域；同领域可以有多个知识源。
每个源的 `id` 必须唯一，`description` 告诉模型资料范围；省略 `source` 时选择支持该领域的首个源。
模型只看到逻辑源、领域和描述，不会看到 URL、凭据、知识库 ID 或患者身份。

`domains` 支持 `health`、`help`、`product`。`scoped_domains` 声明哪些领域需要医院空间：
这些领域必须有可信请求输入的 `spaceId` / `space_id`，由服务填入 `scope.space_id`；缺少时不发送检索请求。
医疗文献不配置 `scoped_domains`，不附带患者身份或医院范围。
后端负责将受信任范围映射到已发布知识库及访问权限，MetaAgent 不维护此映射。

`token_env` 从进程环境读取凭据，也支持直接在同一 `.env` 中定义该变量，进程环境优先。
也可以在单个源对象里设置 `token`，但不能同时配置 `token` 和 `token_env`。
`timeout_seconds` 可按源设置，默认 20 秒，并受整轮及工具超时约束。
修改环境配置后重启服务或重建容器进程；运行中的进程不会自动重载配置。

`META_AGENT__KNOWLEDGE_SOURCES=[]` 保留原本的本地批准语料模式，由 `KNOWLEDGE_CORPUS_PATH` 指定文件。
配置远程源后优先使用远程契约；无证据、权限拒绝或故障不会静默切换到另一个源或本地语料。

## 接口与边界

按源配置的 URL 发送 POST，使用 `Authorization: Bearer <token>` 和请求追踪头 `X-Request-ID`：

```json
{
  "query": "保留用户主体、条件和目标的完整问题",
  "domain": "health",
  "scope": {},
  "limits": {"max_context_tokens": 4000}
}
```

成功或部分覆盖的响应示例：

```json
{
  "status": "ok",
  "context": "[S1] 已发布的原文证据。",
  "sources": [
    {"id": "S1", "title": "资料标题", "locator": "第 12 页", "ref": "稳定的来源引用"}
  ],
  "gaps": [],
  "trace_id": "opaque-trace-id"
}
```

`status` 为 `ok`、`partial` 或 `no_evidence`；`partial` 必须说明资料缺口，`no_evidence` 的原文和来源须为空。
完整 JSON Schema 见 `src/meta_agent/integrations/knowledge-search-v1.openapi.json`。
响应按契约校验、限流式读取体积并保存证据；引用、缺口、来源和 trace 保留在工具结果及原生运行快照中。
知识检索共用现有工具调用预算及并发限制，不自动重试，不跟随重定向。
跨轮重新检索已发布知识，不将仍在 TTL 内的上轮知识结果当作本轮有效证据。

本次只迁移统一接口客户端，不迁移 MaxKB、旧 Portfolio `/v1/retrieve` 实现、索引、重排、场景目录同步或提示词/事件框架重构。
`main.py`、`interactive.py`、`.env`、`.env.local` 保持原样；原有依赖锁定版本及阿里云镜像保留。
Dockerfile 测试阶段追加复制交互脚本，供真实 HTTP 客户端回归使用，运行阶段及镜像源配置保持不变。
`uv.lock` 只增加契约校验所需依赖，`.env.example` 与 README 增补配置说明。
交互客户端的服务端兼容已独立实现，详见 [交互聊天接口兼容](interactive-chat-compat.md)。

## 验证

- 本地完整回归：221 项通过，1 项因未配置 `META_AGENT_TEST_POSTGRES_DSN` 跳过。
- 原有 Dockerfile 的 test 镜像构建通过；容器内回归同样为 221 项通过、1 项跳过。
- 修改文件 Ruff 检查和 `git diff --check` 通过。
- `uv sync --locked --extra dev --extra postgres --extra llm-deepseek` 通过，既有依赖记录及镜像源未改变。
- Wheel 构建通过，包含知识接口 JSON 契约及运行所需模块。
- Git 规范化哈希确认受保护文件未变，README 和 `.env.example` 的原有内容完整保留。
- 测试使用合成患者夹具和 HTTP mock，尚未对真实患者后端或真实知识服务做联调。
