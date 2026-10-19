# dev：MetaAgent 直连 MaxKB

2026-10-19 更新：已接入原生 rerank 工作流。配置步骤与本机结果见
[原生重排落地记录](maxkb-native-rerank-2026-10-19.md)。

2026-10-18。本版取消 bridge 运行时依赖，使用固定版本 MaxKB v2.10.7-lts 的原生管理会话 API。
不新增检索服务，不在 MetaAgent 中转 embedding / rerank 请求，也不挂载定制模型驱动。

```text
调用方 → MetaAgent /v1/knowledge/retrieve → MaxKB hit_test → MaxKB 配置的 embedding
                   ↓ 证据
          MetaAgent 回答模型（可选）
```

MaxKB 负责文档管理、分块、向量化和原生关键词／混合检索。MetaAgent 保留必要的适配：
认证、space_id 与类型选库、发布和引用一致性校验、少量证据返回。原有 Dify 接口继续保留。
默认部署只启动 Agent，复用现有 MaxKB；可选 profile 提供不含 bridge 的 MaxKB 替换容器。

## 原生模型配置与旧 bridge 切换

1. 保留并备份现有 MaxKB 数据卷、发布目录和私有配置，确认实际数据卷名。
   当前 Compose 默认引用外部卷 `metarag-maxkb_maxkb_data`；不会自动新建空库。
2. 如果旧 MaxKB 挂载了 `provider_embedding.py`，先停止旧 MaxKB，再用下方可选 profile
   启动同版本、同数据卷的 MaxKB，恢复镜像自带的模型驱动。**两个 MaxKB 不能同时使用同一数据卷。**
3. 在 MaxKB「模型管理」编辑知识库正在使用的 embedding 模型，配置厂商地址和新的 API Key。
   把旧 `http://bridge:8000/v1` 替换为厂商实际支持的 **OpenAI 兼容 API base**。
   阿里云百炼原生文本 embedding 驱动通过 OpenAI SDK 调用 `/embeddings`；
   DashScope 原生 `/api/v1/services/embeddings/...` 地址不能直接作为这个 base。
   新密钥只填在 MaxKB，不填入 MetaAgent 的配置文件。
4. 保持模型、维度和文档／查询向量语义一致。旧驱动曾区分 query/document `text_type`，
   镜像原生驱动的 `embed_query` 则复用 `embed_documents`；即使模型名和维度相同，
   也需要通过下面的小样本验证兼容性，不能据此保证召回等价。更换模型或维度后需重新向量化。
5. 新 Agent 直连验证完成后停止旧 bridge。旧 Compose、旧 MaxKB 模型地址仍可能引用 bridge，
   不能只停 bridge 而保留旧地址。不要批量启动历史实验栈。

模型保存可能触发 MaxKB 原生连通性校验和少量厂商请求。本版未写入或恢复任何外部模型密钥。
不提供直接修改 MaxKB 数据库凭据的工具，使用原生界面完成配置。

官方依据：[百炼配置说明](https://docs.fit2cloud.com/maxkb/user_manual/model/bailian_model/)、
[固定版本 embedding 驱动](https://github.com/1Panel-dev/MaxKB/blob/v2.10.7-lts/apps/models_provider/impl/aliyun_bai_lian_model_provider/model/embedding.py)。

## 检索策略和 rerank 边界

space_id 必须与发布中的值完全一致；当前为「上海中医药大学附属岳阳中西医结合医院」。
在登录或检索前按 tenant、space、operator 角色选择已发布的物理知识库，调用方不能传入任意知识库 ID。
此 API 只用于持有服务令牌的可信内部调用方，不把令牌交给普通终端自行选择空间。

| 知识类型 | 原生召回 | 单库候选数 |
|---|---|---|
| scene_kb / hospital_kb | keywords | 12 |
| sop_kb | blend | 12 |
| device_kb | blend | 16 |
| clinical_kb / rehab_kb | blend | 24 |
| technical_kb | blend | 20 |

v2.10.7-lts 的原生 `hit_test` 没有 rerank 参数，本版将已授权候选交给 MaxKB 原生高级智能体工作流重排。
场景、医院、纯 SOP 请求跳过重排；设备、医学、康复、技术资料及包含这些类型的混合请求合并后重排一次。
先按单库内部排名交错选候选，再受 48 个候选／250 KB 正文预算限制，不直接比较各库原始分数。
未配置重排应用或调用失败时返回明确错误，不将普通排序标为重排成功。
成功响应包含 `rerank_applied=true`、`reranker_used` 与 `stages.native_rerank_requests`。
embedding／rerank 的厂商实际调用数由 MaxKB 掌握，MetaAgent 只统计原生接口请求数。

官方依据：[固定版本 HitTestSerializer 和 hit_test](https://github.com/1Panel-dev/MaxKB/blob/v2.10.7-lts/apps/knowledge/serializers/knowledge.py)。
这是固定版本的管理端接口适配；升级 MaxKB 前需要重跑接入 smoke，不能视为跨版本稳定的公共 API。

## 准备和启动

从包含本次提交的干净 checkout 构建；当前开发目录还有此前未完成合并的改动，不作为构建输入。
运行时基础镜像仍复用既有 `medical-metaagent:internal-v2`，其他机器需预先提供该镜像。

```powershell
Set-Location D:\Project\metaAgent
.\.venv\Scripts\python.exe -X utf8 tools/prepare_maxkb_dev.py
.\.venv\Scripts\python.exe -X utf8 tools/configure_maxkb_rerank.py --model-id <MaxKB原生重排模型UUID>
docker compose --env-file .local/maxkb-dev/compose.env -f compose.maxkb.yaml up -d --build agent
```

默认连接容器宿主的 `18081/admin/api`。准备脚本只读取 MetaRag 的发布、原文和 MaxKB 管理登录信息，
生成 `.local/maxkb-dev/native.json`（仅 MaxKB 用户名／密码）、`agent.env`、`compose.env` 和 `access.json`。
Agent 只挂载发布文件、对应 catalog、原文和 `native.json`，不会挂载整个私有目录。
脚本不读取旧 `.env`、归档、`bridge.json` 或其他应用的模型密钥；
仅保留目标 `agent.env` 已有的回答模型配置，不会从历史配置补回已清空的密钥。

默认 `--answer-mode disabled`：不需要回答模型密钥即可提供检索 API。
要启用回答，在目标 `agent.env` 配置新的回答模型凭据，再将
`META_AGENT__MAXKB_ANSWER_MODE` 设为 `llm` 并重新创建 Agent。
`extractive` 可用于明确的原文摘录。`disabled` 下调用 Dify 知识问答会返回 503，提示改用检索 API。
若要使用 `--answer` smoke，也需以相同 `--answer-mode` 重跑准备脚本，更新 `access.json`。

需要替换旧 MaxKB 时，在确认并停止占用数据卷的旧容器后：

```powershell
.\.venv\Scripts\python.exe -X utf8 tools/prepare_maxkb_dev.py --maxkb-url http://maxkb:8080/admin/api --data-volume metarag-maxkb_maxkb_data
.\.venv\Scripts\python.exe -X utf8 tools/configure_maxkb_rerank.py --model-id <MaxKB原生重排模型UUID> --agent-admin-url http://maxkb:8080/admin/api
docker compose --env-file .local/maxkb-dev/compose.env -f compose.maxkb.yaml --profile maxkb up -d --build
```

此 profile 固定 MaxKB 镜像摘要，保留原生 PostgreSQL／Redis 和现有轻量启动配置，
不启动本地模型服务。仅适用于这里使用外部文本模型的部署；OCR／图片模型不是本版验证范围。
Agent / MaxKB 的 512 MiB / 2304 MiB 是资源上限配置，**不是实测空闲内存、解析峰值或镜像大小**。
Compose 默认只绑定本机回环端口，局域网发布需要在实际服务器设置绑定地址和 public URL。

## 调用和来源

从 `access.json` 获取 `service_token`，以 `Authorization: Bearer <service_token>` 调用：

```http
POST /v1/knowledge/retrieve
Content-Type: application/json

{
  "query": "电脑端如何跳跃和奔跑？",
  "tenant_id": "maxkb-pilot",
  "space_id": "上海中医药大学附属岳阳中西医结合医院",
  "knowledge_types": ["sop_kb"],
  "top_k": 3
}
```

`knowledge_types` 可省略，此时根据领域和问题选择类型。响应返回 `retrieval-v1` 证据及阶段信息，
包括 `doc_ref`、PDF 物理页码、MD 标题路径、来源、内容哈希和知识类型，不调用回答模型。
独立来源接口为 `GET /v1/knowledge/sources/{code}?tenant_id=...&space_id=...`，
也要求服务 Bearer；普通浏览器直接点链接不会自动携带令牌，应由可信后端转发下载。
下载会再次校验空间、原文哈希和 MaxKB 文档启用状态。

原有 Dify 接口 `/compat/dify/v1/chat-messages`、`/compat/dify/v1/workflows/run` 在启用回答后继续可用。
输入使用原来的 `inputs.tenantId`、`inputs.space_id`、`inputs.role=operator`。
`metadata.knowledge_retrieval.integration=native`，引用仍为 `[doc_ref]`。
`response_verified=true` 指本地发布、原生内容与定位校验通过；本版没有跨 bridge 的 HMAC 签名。

## 更新、禁用和失败恢复

保留 MetaRag 既有离线导入／发布工具及 release/catalog 格式，它们直连 MaxKB 管理 API，
不需要 bridge 进程；旧工具可能仍从名为 `bridge.json` 的私有文件读取管理账号，
不要在未迁移这些离线配置前删除该文件。它不是本版 Agent 的运行时依赖。

- 在 MaxKB UI 禁用／删除文档或段落后，返回证据前会重新检查原生状态，下载原文也检查文档状态。
- 新增或修改内容必须先发布 catalog，再重新准备配置并重建 Agent；未发布的新段落不会进入在线引用。
  已发布段落正文或标题变更返回 409，避免旧页码对应新文本。改动未被本次召回的段落不会阻断其他未变证据。
- 登录会话失效只重新登录重试一次；连接错误／超时返回 503／504，不伪装成成功空结果。
- 运行中的发布版本改变返回 409，需重新创建 Agent；证据或原文校验失败拒绝输出。
  catalog 损坏导致启动失败，运行时校验错误返回 502。

## 少量 smoke

```powershell
.\.venv\Scripts\python.exe -X utf8 tools/smoke_maxkb.py
# 回答凭据已显式配置并启用 llm/extractive 后，可追加回答链路检查：
.\.venv\Scripts\python.exe -X utf8 tools/smoke_maxkb.py --answer
```

默认仅调用检索 API：操作说明、医院场景、医学 PDF、MD 混合类型各 1 个样本，加 1 个越权空间拒绝。
核对有证据、空间／发布一致、PDF 页码、可认证下载来源；结果保存在 `.local/maxkb-dev/native-smoke.json`。
这不是全量召回、语义正确性、性能或内存评测；真实检索仍可能产生 MaxKB embedding API 用量。

2026-10-18 基线的本地 15 项检查通过，使用模拟原生 API 验证接入、Dify 阻塞和 SSE、权限、禁用、
会话恢复、内容防篡改及不恢复旧密钥。局域网断开、外部模型密钥未配置，
该基线当时未完成真实 MaxKB／局域网 smoke，未更新局域网服务。
10 月 19 日的本机真实接入验证另见落地记录；局域网服务没有在本次操作中更新。
旧 `maxkb-dev-smoke-2026-10-18.json` 属于 bridge 方案历史记录，不能作为本版上线证据。
