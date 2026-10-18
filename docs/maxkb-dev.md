# dev 分支接入 MaxKB

2026-10-18。在切换前，codex/exploration 的待保存评测文档与记录已提交为 8aa6436。
本次基于 dev 原有的 LangGraph/Dify 兼容接口接入，不包含整个实验分支的功能合并。

MaxKB 延用 D:\Project\MetaRag 的既有部署与通义 embedding/rerank。
发布为 maxkb-scoped-508a6ce72170，临时 spaceid 为「上海中医药大学附属岳阳中西医结合医院」。
MaxKB 负责检索，MetaAgent 使用既有回答模型生成带 doc_ref 引用的答案。

## 启动与调用

~~~powershell
Set-Location D:\Project\metaAgent
.\.venv\Scripts\python.exe -X utf8 tools/prepare_maxkb_dev.py
docker compose --env-file .local/maxkb-dev/compose.env -f compose.maxkb.yaml up -d --build
docker compose --env-file .local/maxkb-dev/compose.env -f compose.maxkb.yaml ps
~~~

既有 MaxKB 和 bridge 需已启动。这里只新增一个 Agent 容器，使用内存持久化，
上限 512 MiB/1 CPU，端口 18084；原有 5080 主服务未切换。
镜像复用本机 medical-metaagent:internal-v2 已安装的依赖，通过 PYTHONPATH 加载本分支 src。
基础运行时在其他机器需预先提供，不能假设这个本地镜像可从公共仓库下载。
限额是配置，不代表实测空闲内存或峰值。

当前沿用 dev 的入口：

~~~text
POST http://127.0.0.1:18084/compat/dify/v1/chat-messages
POST http://127.0.0.1:18084/compat/dify/v1/workflows/run
~~~

Bearer 使用 .local/maxkb-dev/access.json 的 service_token。阻塞请求例子：

~~~json
{
  "query": "请检索知识库：电脑端如何跳跃和奔跑？请引用来源。",
  "user": "maxkb-operator",
  "inputs": {
    "tenantId": "maxkb-pilot",
    "role": "operator",
    "space_id": "上海中医药大学附属岳阳中西医结合医院"
  },
  "response_mode": "blocking"
}
~~~

answer 保留 [answer] 兼容标签；metadata.citations 包含实际引用的文档、下载来源、物理页码、
空间和类型，metadata.knowledge_retrieval 返回发布、领域、检索次数和校验结果。
SSE 在 message_end 的 metadata 返回相同信息；Workflow 在 outputs.metadata 返回。
物理页码以 citations.page 为准，正文里的页眉/页脚数字可能是印刷页号。

这是 operator 内部知识调用，目前不接受患者身份上下文。
输入来自受 Bearer 认证的可信服务网关，不能直接向普通终端暴露服务令牌并让其自行选择角色。
缺空间返回 422，其他空间/角色返回 403；在读取会话检查点或请求检索前校验。
会话与证据隔离键已纳入空间，防止同一用户跨空间复用状态。

## 接入行为

MAXKB_ENABLED 默认 false。启用后，带知识库、文献、共识、场景、操作等明确关键词的问题
由图中的 knowledge_search 任务调用 MaxKB；其余请求继续走 dev 原有工作流。
这是首版启发式路由，不承诺覆盖任意自然语言的意图识别或多任务混合请求。
help/product/health 领域映射到 bridge 已有的类型策略，调用方不能让模型自行注入空间或原生知识库 ID。

检索请求绑定发布版本、内容哈希和索引哈希，验证 HMAC 与每条证据的定位/正文哈希，
之后才将少量证据交给回答模型。回答中的引用必须属于本次返回的证据。
该校验保证引用身份，不是语义正确性或全面医学质量评审。
检索失败、签名错误或模型失败分别返回错误，不以另一份无来源回答冒充成功。
extractive 模式可明确配置为原文摘录；此次真实 smoke 使用 llm 模式，没有自动摘录降级。

MaxKB 资料更新后，先在 MetaRag 发布，再重新运行 prepare_maxkb_dev.py 并重建 Agent，
使其重新绑定发布。凭据及真实响应保存在 .local，已从 Git 和镜像构建上下文排除。

## 必要 smoke

~~~powershell
.\.venv\Scripts\python.exe -X utf8 tools/smoke_maxkb.py
~~~

只验证 3 个真实正样本与 1 个边界，不做全量召回或性能评测：

| 样本 | 实际链路 | 结果 |
|---|---|---|
| 电脑端跳跃和奔跑 | MaxKB 操作说明 → 现有模型 → 引用 | HTTP 200 |
| 岳阳医院二楼场景 | MaxKB 场景说明 → 现有模型 → 引用 | HTTP 200 |
| 2025 智能康复共识 | PDF → 通义 rerank → 现有模型 → 页码引用 | HTTP 200；引用物理页 1、2 |
| 未授权其他空间 | 请求在检索前拒绝 | HTTP 403 |

PDF 文献不同段落出现 18/20 项的表述，模型本次说明了原文差异；未将答案评定为全面正确。
本次未测患者会话、生产流量、全量召回或跨类型质量排序。
13 项本地接入/兼容性检查通过，覆盖 HMAC 防篡改、空间拒绝、SSE 元数据和既有 Dify 接口。
完整样本响应、调用次数及耗时在 .local/maxkb-dev/smoke.json。

## Docker 负担调整

本机本次启动时有 27 个容器同时拉起，Dify 插件容器出现重启循环。
已停止当前任务不需要的 21 个容器并关闭其自动重启：
Dify、医生沙箱、旧医学 RAG、模拟器及旧 MaxKB Agent。
保留 MaxKB/bridge、原主 Agent 及其依赖，新增 dev Agent 后当前共 7 个运行容器。
没有删除镜像或数据卷。检查时新 Agent/MaxKB/bridge 的 RestartCount=0、OOMKilled=false，
这只能说明观察时状态，不能据此断言历史闪退的根因已消除。

原容器名及重启策略已记录在 .local/maxkb-dev/docker-reduced.json。
需恢复某个项目时，按记录恢复 docker update --restart 策略后 docker start，
或使用原项目 Compose 明确重建；不要一次启动全部历史实验栈。
