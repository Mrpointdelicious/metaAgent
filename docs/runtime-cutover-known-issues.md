# Runtime Cutover Known Issues

本文件记录 single-agent runtime baseline 的非阻塞问题。它们不在本轮收口中实现或重构。

## KI-01 Knowledge Backend

当前 `search_knowledge` 路径为 `KnowledgeAdapter → 本地 approved JSON corpus → BM25`，
只检索经过批准的领域分节，属于简化实现。未配置可用语料或无匹配时返回 unavailable。

后续候选为 `KnowledgePort` 下的 `DifyRetriever` 或 Independent RAG Service。
这些组件尚未实现。Dify RAG、Hybrid Retrieval、Reranker 及生产级知识服务留给独立 feature；
Dify 方向使用 `feature/knowledge-dify`。

## KI-02 Structured Tool Result Reuse

已有多轮文本记录显示：部分自然语言上下文可复用，部分已查询的结构化训练结果在后续轮
未被复用或再次查询。当前历史按轮次与 token 预算裁剪，并检查工具 Evidence 时效。

这不阻塞 runtime cutover。后续可单独考虑 evidence anchor、structured state 和
tool-result reuse policy，本轮不调整 Context 管理或 Prompt。

## KI-03 Medical Quality

当前测试覆盖工具选择、合法参数、数据获取、Evidence、多轮、Artifact 和失败降级。
它们不验证临床诊断或康复建议的医学正确性。真实建议问答仅保留结果，不能据此声称医学质量已验收。

## KI-04 Scaling

当前仅支持 single worker、single replica。生产门限制 worker 数量，数据库实例锁阻止同一数据库
上的第二实例启动。面向多实例的运行租约和 event replay 不在当前 v1 范围内。

## KI-05 Companion AI_WebApi Deployment

三层患者工具依赖配套 AI_WebApi 的 `patient-layers-1.0.0` 接口。
该后端不属于此 MetaAgent 仓库的部署产物。已有真实验证使用独立本地实例，现有 5043 服务
尚未部署这次三层接口更新。实际部署必须同步提供兼容的 AI_WebApi，不能仅更新 MetaAgent。

这是部署前提，不阻塞本仓库代码合并。本轮不替换服务或重新执行大型真实数据 Eval。

## KI-06 Tool Budget Accounting

`MAX_TOOL_CALLS` 当前计量经过 `RunContext.call()` 的后端调用，包含按需身份映射。
本地 `search_knowledge` 不经过该计数器，仍受模型调用次数和 request timeout 限制。

本轮确认并保留现有计量语义，没有解除调用预算。若后续需要为所有 Structured Tool 执行
统一计数，应另行设计与验证，不在本轮修改。
