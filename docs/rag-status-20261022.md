# RAG 现状检查（2026-10-22）

本次仅执行现状检查和正常检索，未修改 RAG 服务源码、配置、语料、索引或发布状态。正常检索会生成服务自身的 retrieval trace。MetaAgent 的知识适配器和场景目录文件哈希检查未发生变化；`D:/Project/rag-portfolio` 的 Python 源码哈希和 Git 状态检查也保持一致。

## 当前接入

`Dify → MetaAgent /v1/chat → search_knowledge → PortfolioKnowledgeAdapter → rag-portfolio /v1/retrieve`。

运行服务是 `rag-portfolio-api-1`，MetaAgent 内部访问 `http://rag-portfolio-api:8000`。存储为 PostgreSQL + Qdrant；检索使用 dense/sparse 混合召回、融合排序与可选重排。当前实际模型配置为阿里云 DashScope 的 `qwen3.7-text-embedding`（1024 维）与 `qwen3.7-text-rerank`。RAG 返回原文证据，`answer_mode=evidence`；最终回答由 MetaAgent 的 DeepSeek 模型组织，RAG 自身未配置回答模型。

| spaceId | domain | 知识库 | 活动发布 | 发布文档数 |
| --- | --- | --- | --- | ---: |
| 上海中医药大学附属岳阳医院 | health | rehab_kb | a10017f1-2e81-40f3-a5fc-b2e9304d87f1 | 8 |
| 上海中医药大学附属岳阳医院 | help / product | hospital_kb | 57adfcfe-fdbb-433c-a3c1-2e3066501523 | 5 |

两库均 enabled，发布状态均 active。文档数是发布清单总数；各凭据实际可见内容还受 audience 约束。

`spaceId` 在 MetaAgent 中映射为受信任的 KB ID；RAG 请求以 KB ID 限定范围，不额外传入 `space_id` 或患者身份字段，库 ID 不由模型指定。自然语言 query 本身仍可包含医院名称等提问内容。`sceneId` 用于当前场景上下文并校验归属，不用于缩小知识检索范围，符合此前约定。

医院 help/product 使用患者可见凭据，health 使用 internal audience 凭据；医生专属说明不向医院患者检索凭据开放。RAG 按审核、生效期、撤销状态、活动发布与 audience 校验文档资格，MetaAgent 再核对返回原文的 SHA256。

## 本次实测

- `/health/ready`：PostgreSQL schema、Qdrant 均为 ok。
- 适配器直接检索：两个不同 sceneId 的医院查询均成功，各返回 5 条证据、使用相同医院 KB；康复文献查询成功并返回 PDF 页码。
- 请求验证：3 个非法场景/医院组合被拒绝，未知 spaceId 不回退到默认库。
- Dify 已发布入口询问运动评估室位置：MetaAgent 状态 succeeded，返回四楼评估中心及医院场景说明来源。workflow run：`71b248e9-b747-4696-ad59-e6da1e8103b6`；RAG trace：`6b47df70-a033-4b1d-8047-a54530e4235c`。
- Dify 已发布入口检索脑卒中站立平衡评估：MetaAgent 状态 succeeded，返回带文献与 PDF 页码的证据。workflow run：`329f5ff6-017d-46d7-9f89-97113cb64666`。这证明链路有效，不代表完成医学答案质量验收。
- 上述正常检索 `notes=[]`，本次未发生重排降级。未停止服务、未执行故障注入。

## MaxKB 残留

仍存在历史代码与部署材料，未清理：

- `src/meta_agent/domains/maxkb*.py` 共 6 个模块；旧 `graph/builder.py` 仍导入 MaxKB 能力。
- `config.py` 的旧配置项，以及 `api/knowledge_routes.py` 的兼容路由。
- `tools/prepare_maxkb_dev.py`、`configure_maxkb_rerank.py`、`smoke_maxkb.py`。
- `Dockerfile.maxkb`、`compose.maxkb.yaml`、`deploy/maxkb/`、历史文档和测试。

现用装配链是 `infrastructure/container.py → graph/runtime.py` 与 `SingleAgentRuntime`，不走旧 builder；运行配置 `maxkb_enabled=false`，没有运行中的 MaxKB 容器。已鉴权实测旧 `/v1/knowledge/retrieve` 返回 `503 / maxkb_disabled`。RAG 失败不会回退到 MaxKB。当前仓库另保留本地 JSON/BM25 检索实现，仅适用于未启用 Portfolio 的配置，不是当前部署的知识来源或故障回退。

旧 MaxKB 默认地址的 18081 端口现在被 EvalHub 使用。现链路不会访问它，但残留实验脚本和默认配置易造成误用，后续可单独清理。

## 不足及优先级

1. **低相关性资料仍可能被返回。** 实测向医院库问“量子计算机的纠错阈值是多少”，仍返回 5 条医院操作说明，最高重排分约 0.097，`notes=[]`。在线检索路径未设置相关性拒绝阈值，MetaAgent 适配器也未保留 score 用于质量判断。需要单独设计无答案判定、低分拒绝与评测；当前不能把“返回了证据”等同于“资料能回答问题”。本次未改。
2. **worker 与当前 API/源码不同步。** 运行 worker 可处理 `system.probe`，但代码检查缺少 `document.ingest`、`release.build` 处理逻辑和 `services.releases` 模块。数据库现有记录为入库成功 9、发布成功 4、probe 成功 1，没有积压；这不能证明当前旧 worker 能承接新的入库/发布。现有索引检索不依赖 worker，故当前查询正常。本次没有投递任务或重启 worker。
3. **场景仍是静态目录。** RAG 的 `scene_space`、`scene_snapshot`、`scene_node` 均为 0 行；MetaAgent 依据已批准场景说明维护 34 个原始名称。语料更新后不会自动同步场景校验目录；在线 retrieve 当前不支持 `registry_snapshot_id`。应后续统一场景编码、发布和同步机制。
4. **覆盖与检索策略有限。** 现活动发布共 13 份文档；每次适配器只发一个 query，candidate_k=40、top_k=5、context=4000，尚未在此链路引入复杂问题拆解和多轮检索质量验收。
5. **身份与展示尚可细化。** 医院查询固定使用患者可见凭据；若需医生专属资料，需按可信身份设计权限映射。Dify 能展示标题与页码/原文分段，但尚无直接打开对应原文位置的入口，语句与具体证据的对应也未做强校验。另外，纯知识问题仍会自动查询患者基本档案，带来额外上下文和延迟，可后续评估按意图加载。
6. **历史实现尚未退场。** MaxKB 模块、配置、兼容接口和旧 graph 并存，维护与误用风险仍在，宜单独安排清理，不与检索行为调整混在一起。

本轮报告对应本机运行状态。详细原始验证记录保存在 Git 忽略目录 `.local/image-rag-check/`，其中包含私有测试信息，不纳入源码交付。
