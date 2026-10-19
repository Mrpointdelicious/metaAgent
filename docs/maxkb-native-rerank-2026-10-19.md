# MaxKB 原生 rerank 落地

日期：2026-10-19。按 [设计稿](maxkb-rerank-design.md) 实现，取消 bridge 运行时依赖。

## 运行链路

```text
MetaAgent /v1/knowledge/retrieve
  → 校验 tenant / space_id、按知识类型选库
  → MaxKB 原生 hit_test
  → 校验候选内容、发布版本和启用状态
  → MaxKB 正式发布的 rerank-only 应用（需要时）
  → 复查状态、生成 PDF 页码／MD 标题引用
```

embedding 和 rerank 的地址、模型、供应商密钥由 MaxKB 原生模型管理负责。
MetaAgent 只用已有 httpx 发送请求，没有新增模型 SDK、模型权重或常驻转发服务。
纯场景／医院信息／SOP 跳过重排；需要重排的类型及混合类型请求统一重排一次。
失败返回 502／503／504，不把普通召回标为重排成功。

## 原生模型与工作流

用户本次明确授权使用历史通义密钥，因此只把该密钥配置到 MaxKB 原生模型中。
MetaAgent 的回答模型密钥未恢复，当前 dev 仍以 `answer_mode=disabled` 提供独立检索 API。

| 项目 | 当前配置 |
|---|---|
| embedding 模型 | qwen3.7-text-embedding，1024 维 |
| embedding API base | https://maas.qianwenaiapi.com/compatible-mode/v1 |
| rerank 模型 | qwen3.7-text-rerank |
| rerank API base | https://maas.qianwenaiapi.com/api/v1 |
| 原生高级智能体 | MetaAgent Native Rerank |
| 应用 ID | 01a1519c-7fd7-7f40-8aa3-c7cb6dcfbae1 |
| 执行节点 | 开始 → 原生 reranker-node → JSON 指定回复 |

应用不关联知识库，不使用 LLM，不启用长期记忆／文件上传／工具／MCP。
已关闭匿名分享，应用 API Key 的正式调用路径仍可用；错误应用 Key 已验证被拒绝。
通过 MaxKB 原生设置将 `clean_time`／`file_clean_time` 设为 1；原生会话记录的磁盘增长未做评测。

原始工作流图保存在 [模板](examples/maxkb-rerank-workflow.json)，不包含真实密钥。
应用版本和工作流摘要保存在私有 `rerank-application.json`，用于部署复核。
该应用使用正式发布 API，未暴露调试端点或私有模型端点。

## 配置与再次部署

在 MaxKB 原生 UI 新建或修改重排模型后，使用其 UUID：

```powershell
python tools/prepare_maxkb_dev.py --maxkb-url http://maxkb:8080/admin/api
python tools/configure_maxkb_rerank.py --model-id <原生RERANKER模型UUID> --agent-admin-url http://maxkb:8080/admin/api
docker compose --env-file .local/maxkb-dev/compose.env -f compose.maxkb.yaml --profile maxkb up -d --build
```

从包含本次改动的干净代码构建。当前开发工作区原有未完成合并内容没有纳入这次镜像或提交。
准备脚本只保留本部署的 MaxKB 应用凭据，不从历史文件恢复供应商密钥；
这次按授权使用历史通义密钥的操作只执行在 MaxKB 原生模型配置，不存在于准备脚本中。

私有 `native.json` 的新增字段为：

```json
{
  "rerank": {
    "enabled": true,
    "api_base": "http://maxkb:8080/chat/api",
    "application_id": "原生高级智能体UUID",
    "api_key": "由MaxKB生成的应用Key",
    "timeout_seconds": 8
  }
}
```

`configure_maxkb_rerank.py` 自动创建／更新受本工具管理的应用、发布工作流并写入该配置；
不会改动其他应用，也不会创建、读取或转存供应商密钥。
重跑准备脚本会保留应用 Key，并让应用 API base 跟随当前 MaxKB 地址。
重跑配置工具会复用自己的应用和 Key，不因相同工作流重复发布版本。

## 接口与校验

对调用方仍是 `POST http://127.0.0.1:18084/v1/knowledge/retrieve`，使用既有服务 Bearer。
当前 tenant 为 `maxkb-pilot`，space_id 为「上海中医药大学附属岳阳中西医结合医院」。
其他空间在任何原生召回请求前拒绝；模型不能注入知识库 ID。

重排最多提交 48 个候选，每个正文窗口不超过 1,600 字符，总候选序列化预算 250 KB。
相同标题加正文只发一个确定来源，避免原生驱动按文本恢复 metadata 时产生歧义。
本次请求单独创建原生会话，验证 request_id、结果成员关系、ID 唯一性、内容／哈希和有限分数。
引用由本地冻结 catalog 生成，不使用原生重排拼接或截断后的 page_content 冒充原文。
重排前检查候选启用状态；返回证据前再检查一次，防止重排期间的禁用继续进入返回结果。

`rerank_applied` 表示确有重排证据通过引用复核，`reranker_used` 标识原生应用。
`stages.native_rerank_requests` 表示工作流请求数；需要重排时不填报未知的厂商实际调用／重试次数。
默认最多两个检索并发，原生重排预算 8 秒并受检索总超时约束；不自动重试重排请求。
超时只代表 Agent 停止等待，不保证供应商已停止执行。

## Docker 切换与数据

复用 `metarag-maxkb_maxkb_data` 卷。已确认 PostgreSQL 实际数据目录为
`/opt/maxkb/data/postgresql/pgdata`，位于该卷中；未把空的匿名 PostgreSQL 声明卷误当作业务数据卷。
旧 MaxKB 停止后才启动替换容器，避免两个数据库进程同时使用一个数据目录。
替换容器使用固定 MaxKB 镜像摘要和仓库启动脚本，不挂载旧定制 embedding 驱动。

切换前保存了私有 PostgreSQL 备份、应用加密 secret 和旧容器配置。
它们位于 `.local/maxkb-rerank-impl/`，含私有凭据，只用于本机恢复，未进入 Git 或镜像构建上下文。
尚未验证的回退动作不要在运行中的新数据库上直接恢复备份。

本次只更新本机 dev 端口 18084 和 MaxKB 18081；没有更新局域网服务器或旧 5080 Agent。
本机 Docker 的 socket 启动故障修复仍见 [独立记录](docker-desktop-recovery-2026-10-18.md)。

## 必要验证

本地 20 项接入／兼容性检查通过，包括：原生重排成功、每次独立会话、错 request_id／外来候选拒绝、
供应商失败不泄露回包、空间拒绝、禁用文档、Dify 阻塞／SSE 与来源认证。
Docker 镜像从干净的 HEAD 加本次改动构建，通过 Compose 配置检查。

真实供应商探测已确认 embedding 返回 1024 维，rerank 返回原文。
正式发布应用的真实调用将“电脑端按空格跳跃”排在无关医院导航之前，错误 Key 被拒绝。
后续少量文档 smoke 的最终结果保存在 `maxkb-native-rerank-smoke-2026-10-19.json`。
这些是接入验证，不能用作全量召回率、普遍质量提升、内存峰值或吞吐测试结论。

最终 5 类真实 smoke 通过：SOP、场景均跳过重排；PDF 与 MD 混合请求执行原生重排且来源下载通过；错误空间返回 403。
首次场景请求出现一次 503，具体原因未确定，定向复测通过；没有将这次失败隐藏为成功空结果。
停用旧 bridge 后又定向复测场景与 PDF，两者通过，验证新链路独立运行。
数据库核对确认发布包中的 204 份文档和 10,025 个分块 ID 全部保留。
新 Agent／MaxKB 的 RestartCount=0、OOMKilled=false，仅代表本次观察窗口。
