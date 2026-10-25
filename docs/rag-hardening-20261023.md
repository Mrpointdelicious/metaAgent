# RAG 加固部署记录

日期：2026-10-23。对应 Dify 应用 `747e56ab-5f99-45ac-b511-a2b960f249c5`。
MetaAgent 源码为 `D:\Project\metaAgent`；知识库服务源码为 `D:\Project\rag-portfolio`。

后续已扩充至 17 份医疗资料，见 [医疗知识发布记录](medical-publication-20261023.md)。
本文的 8 篇医学文献及 workflow ID 记录加固完成时的状态。

## 当前行为

- 医疗知识全局检索 `PORTFOLIO_KB_IDS`，忽略空间映射中的 health；医疗库禁止绑定 space_id。
  当前全局库 `25182bb2-92fc-411a-aff6-6317de7eef77`，保留原有 8 篇活动文献。
- 医院 help/product 按可信 spaceId 选择，sceneId 仅提供当前场景上下文并校验归属。
  当前医院库保留 5 份文档。已审核的场景说明派生 34 个房间名称，与旧审核快照逐项核对一致。
- 场景从已审核来源生成草稿，管理员审核差异和内容 hash 后，随医院知识 release 一起原子激活。
  MetaAgent 从 RAG 已发布目录刷新，默认 30 秒；Dify 场景字段为文本输入，服务端校验归属。
  启用远程目录时不回退到随包静态快照。来源撤销或过期后目录不再可见。
- 重排模型固定为 qwen3.7-text-rerank，相关性策略 qwen37-relevance-v1。
  hospital_kb 阈值 0.502267、rehab_kb 阈值 0.569351；重排关闭或故障不使用 RRF 分数替代。
  模型或未校准知识库类型不匹配时明确报错。保留分数、策略版本、子问题覆盖和 trace_id。
- 每轮最多 4 个检索子问题，同领域最多 2 次调用，总证据预算 4000 token；无证据部分必须说明缺口。
- API 与 worker 使用相同镜像、构建标识和共享资产目录。新入库/构建请求要求处理健康检查通过，
  核对 worker 心跳、版本、支持任务和 PDF 依赖。检索健康检查与处理能力独立。

## 部署版本

| 项目 | 当前值 |
|---|---|
| MetaAgent 镜像 | meta_agent:rag-hardening-20261023-r2 |
| RAG API/worker 镜像 | rag-portfolio:hardening-20261023 |
| RAG 构建标识 | hardening-6053b92a0eaf1240 |
| Prompt | builtin-v3 |
| Prompt SHA-256 | 8b81b156cc0103f3d9416f3c44f2458af43eec5e698ada50a1979e2c6f1be725 |
| Dify 已发布 workflow | 3ca16603-ed8d-40ef-b2ec-c74f623ef707 |
| 医院 release | 04136290-6ccb-422b-a59a-98024e496047 |
| 场景 snapshot | 2cfb57e2-13bb-4278-a562-41c2e1ae3e7b |
| 医疗 release | a10017f1-2e81-40f3-a5fc-b2e9304d87f1 |

医院新 release 沿用原有 5 个已审核文档版本，由实际 worker 构建并经索引校验后激活。
发布前医院 release 为 `57adfcfe-fdbb-433c-a3c1-2e3066501523`。

## 验证及实际限制

- MetaAgent：302 passed、15 skipped；RAG：306 passed，含真实临时数据库迁移及集成测试。
- 相关文件 Ruff 检查通过；运行容器与源码 SHA-256 核验：MetaAgent 119 个文件，RAG API/worker 各 70 个文件匹配。
- 固定 100 道检索正反例：80 道用于校准、20 道预留验证。部署后 50 道相关题均接收、
  50 道无关题均拒绝，预留验证 20/20；没有按验证集结果再次调阈值。
  数据及校准来源在 RAG 的 `eval/relevance-v1/`，原始运行记录位于本地忽略目录。
- 原生真实模型请求验证：无空间、未知空间、当前医院空间均检索相同全局医疗库；
  复杂问题实际调用 2 次、共 4 个子问题、使用 2526 token；无关医院题返回 0 条资料；无效场景 HTTP 422。
- Dify 已发布入口：中文文献、连续追问重新检索、英文系统综述和当前房间上下文均通过。
  引用展示保留原文标题及 PDF 页码；医院文本按原文章节引用。
- 实际 worker 对隔离的合成 Markdown/PDF 资料完成入库、审核、索引构建、发布和实时检索。
  PDF 保留页码/定位锚点。验证库已停用，保留审计历史，不进入正常医院或医疗查询。
- 5776 报告图片回归：生成 1 张，PNG HTTP 200，Dify Markdown 展示通过。
  该次完整报告运行仍为 partial，含两次后端 HTTP 400；本轮不据此宣称所有报告子任务通过。

这些结果验证当前检索边界和部署链路，不等于医疗答案临床准确率。
当前 8 篇医学文献集中于卒中平衡、步态、机器人及膝骨关节炎；复杂问题质量、医学近邻无答案题、
更广主题及独立答案审查仍需扩充。已有合并候选语料的历史待改进题不在本轮“100/100”分母中，未宣称已修复。

## 文献扩充评估

仓库既有 `artifacts/public-medical-corpus-v1/manifest.json` 另存 6 篇去重候选，
包含随机对照研究、综述、步态关联和康复活动剂量研究；均仍标记 clinical_approval=false。
可以作为下一批 8→14 篇候选的起点，但主要补充现有卒中/机器人主题的证据层次。

主题扩展建议先补吞咽、认知和上肢功能，再扩展老年跌倒与其他神经康复疾病。
候选来源：涵盖多领域卒中康复的 [NICE NG236](https://www.nice.org.uk/guidance/ng236/chapter/Recommendations)，
老年跌倒评估与预防的 [NICE NG249](https://www.nice.org.uk/guidance/ng249/chapter/Recommendations/)，
以及包含帕金森、脑损伤和脊髓损伤等病种的 [WHO 神经康复干预包](https://www.who.int/publications/i/item/9789240071131)。
这些仅列为待审来源，未在本轮下载全文、审核或发布到当前知识库。

下一批先核对版本、来源及正文处理条件，按 DOI/作品去重，保留出处和适用人群，
经正式内容审核后走已验证的入库发布链路。新增主题需补充近邻无答案题、跨文献比较和独立保留集，
重跑原有回归，再决定阈值和文献发布；所有医疗资料继续使用全局检索，不绑定医院或房间。
