# Runtime Cutover Merge Readiness

仓库：`Mrpointdelicious/metaAgent`。分支：`feature/runtime-cutover`。
本轮只做 master 同步、回归、敏感资产保护和文档收口。

## Commit

```text
feature HEAD before sync: cf4bc87de07018f6df513076483db860ec0e73d0
feature HEAD at verification: fee60dd6d678a8b6648a9013241977a66d819024
master HEAD (origin/master): ee2669c10ace3ab94c93ddabd54689d5bfe8e145
merge commit: fee60dd6d678a8b6648a9013241977a66d819024
```

Fetch 后，feature 相对 origin/master 为 ahead 1 / behind 1。
执行 `git merge origin/master`，使用 ort 合并成功，无冲突，未丢弃 master 修改。
同步后为 ahead 2 / behind 0。回归针对上述合并后的代码执行。

本报告随后续 `chore: close runtime cutover milestone` 提交发布；该提交仅包含忽略规则、
README 与本轮文档，运行时代码保持验证时版本。发布后的 feature HEAD 以
`git rev-parse origin/feature/runtime-cutover` 为准，避免在提交内容中自引用尚未生成的提交哈希。
本轮只推送 feature，不改写公开历史、不 force push、不自动合并 master。

## Scope

已核对当前 baseline：

- LangChain `create_agent` / LangGraph 单 Agent 为默认 runtime。
- 三层患者工具及 IReTour、IReGo、医院运营、场景、医生、Knowledge 工具接入。
- 患者身份由认证网关和运行上下文注入；Structured Tool 不暴露可自由填写的患者 ID。
- 合法完整工具结果保存在按可信作用域隔离的 Evidence Store。
- 模型读取裁剪后的工具结果；保留多轮消息、Evidence 时效、Checkpoint 与持久化接口。
- 工具错误以 partial / unavailable / failed / unsupported 等状态返回，允许继续使用已有信息。
- 保留模型调用预算、后端调用预算、工具超时与总 request timeout。
- `ORCHESTRATION_MODE=legacy` 保留旧链路回退。

收口修改仅为 `.gitignore`、`.dockerignore`、README、本 Known Issues 与 Readiness 文档。
无业务代码、Prompt、测试 assertion 或 Agent 设计修改。

## Verification

| 检查 | 本轮结果 |
| --- | --- |
| `uv run pytest` | 138 passed / 0 failed / 1 skipped，pytest 报告耗时 2.46s |
| `uv run ruff check src tests` | PASS |
| `uv run ruff format --check src tests` | PASS，104 files already formatted |
| 默认模式 | PASS，Settings 默认值为 agent |
| Agent 容器装配 | PASS，使用合成模型构造真实 create_agent 图，未访问真实模型或数据库 |
| legacy 容器装配 | PASS，旧图仍可装配，未展开业务质量测试 |
| optional PostgreSQL integration | Environment-dependent skip：未提供 `META_AGENT_TEST_POSTGRES_DSN`，不视为 failure |
| 生产门配置验证 | PASS，见下表 |
| Git diff whitespace | PASS |

完整回归包含 single-agent、runtime reliability、native API、IReTour/IReGo、三层工具参数及身份隔离、
knowledge/evidence、production gate 和容器测试。未删除测试、放宽 assertion 或新增 skip。
PostgreSQL 集成测试仍仅接受独立 `metaagent_test` 库；未为本次验收临时创建数据库。

### Production Gate

阅读 `Settings.production_issues()` 与现有生产门测试，并以合成配置逐项确认：

| 生产要求 | 结果 |
| --- | --- |
| 禁止 DRY_RUN=true | PASS |
| Service Bearer Token 必填 | PASS |
| AI_WebApi Bearer Token 必填 | PASS |
| PostgreSQL persistence | PASS |
| PostgreSQL DSN 必填 | PASS |
| 禁止生产 AUTO_SETUP_PERSISTENCE / 启动时 DDL | PASS |
| Agent 模型 API Key 必填 | PASS |
| 单 worker 限制 | PASS |

没有修改或解除这些安全门。

### Existing Live Evidence

以下数字来自已经保存的真实结果，本轮只检查文本与统计，不重新执行模型业务 Eval：

| 已有验证 | 结果 |
| --- | --- |
| Single-turn Agent smoke | 10/10 |
| Multi-turn | 11 succeeded / 1 partial / 0 hard failures，共12轮 |
| Patient-layer direct calls | 12/12 successful，4个对象 × 3层 |
| 原始报表图片 | 5份已保存，含两类设备的单次与趋势报表及跨轮报表 |
| 配套 AI_WebApi 历史回归 | 347 passed；不是本轮重新执行的后端测试 |

partial 来源于 Knowledge unavailable，已有患者事实仍可回答。
数字依据为本地私有 `conversations.json`、`direct-layer-tools.json` 和后端 TRX。
本报告不包含患者姓名、手机号、实际患者编号、医疗记录或图片。
真实数据及本地联调脚本均不进入本次 Git 提交。没有进行视觉或医学质量复核。

## Security and Documentation

- 检查已跟踪路径、内容及 feature/master diff，无已提交 `.env`、真实密钥、令牌、数据库密码、
  真实手机号、患者姓名/完整身份信息、Excel、真实患者 dump、报表图片或本地数据库文件。
- 文本扫描候选已核对为示例占位符、显式合成测试夹具或依赖锁文件哈希；未发现本地 `.env`
  中非占位凭证在已跟踪文件内的精确匹配。未复制敏感值到报告。
- `.gitignore` 已覆盖 `outputs/`、本地私有测试脚本与历史工作文档；真实资产在本地保留。
- `.dockerignore` 同步排除 outputs 与 manual，防止这些本地资产进入 Docker build context。
- README 已准确说明默认 runtime、主要工具、`KnowledgeAdapter → local approved JSON corpus → BM25`、
  单 worker/单副本、结构化结果可能重查、医学质量范围及 Dify 非主路径状态。
- README 中未跟踪脚本与旧文档的链接已更正，没有宣称 Dify RAG 或 rag-portfolio 已实现。

## Runtime Boundaries

```text
ApplicationService
  → SingleAgentRuntime
  → LangChain create_agent
  → Structured Tools
  → Domain / AI_WebApi Adapter
```

| 边界 | 检查结论 |
| --- | --- |
| 身份 | trusted inputs → TrustedScope → runtime context；tool payload 中的 system_context 由系统注入 |
| Tool | 严格业务 schema；可见工具按身份/配置过滤，执行时再次检查 tool_allowed |
| Evidence | 完整合法响应先写入作用域 Evidence Store，再裁剪供模型读取 |
| Context | compact_result、bounded_history、fresh_history 与总体 context overflow 检查仍在 |
| Failure | invoke_tool 捕获领域错误/超时并返回业务状态，ApplicationService 保留终态事件 |
| Model budget | LLMBudget.take() 限制次数，最后一次模型调用移除工具 |
| Tool budget | RunContext.call() 限制后端调用；本地 Knowledge 的计量差异记录为 KI-06 |
| Timeout | ApplicationService 总时限及各工具 timeout 仍有效 |
| Fallback | Container 根据 orchestration_mode 选择 Agent 或旧图，两种装配均通过 |

这里只核对边界，没有重构 Context、legacy 或 KnowledgeAdapter。

## Known Issues

详见 [Runtime Cutover Known Issues](runtime-cutover-known-issues.md)：

- KI-01：本地 approved JSON corpus + BM25 简化 Knowledge backend。
- KI-02：部分结构化训练结果跨轮未复用或再次查询。
- KI-03：临床诊断及康复建议的医学正确性未验收。
- KI-04：single worker / single replica。
- KI-05：实际部署需同步提供配套 AI_WebApi 三层接口。
- KI-06：MAX_TOOL_CALLS 为后端调用计数，本地 Knowledge 另受模型预算和总时限约束。

以上属于明确记录的后续工作或部署前提，不阻塞本次 runtime baseline 合并。

## Explicitly Out of Scope

- Dify RAG / Dify Retriever / rag-portfolio。
- production-grade Knowledge Service、Independent RAG、Hybrid Retrieval、Reranker。
- medical quality eval。
- multi-worker / multi-replica scaling、分布式运行租约、event replay。
- Admin UI 或其他新 UI。
- new tools、新业务功能、Prompt 质量优化、Context 或 legacy 大规模重构。

Dify RAG 须在本次合并之后由独立 `feature/knowledge-dify` 推进。

## Decision

READY TO MERGE

Blocking issues：无。此结论针对经过回归的 Runtime Cutover baseline，不代表生产部署或医学质量已验收。

推荐 PR 标题：`feat: switch default runtime to LangChain single-agent`。

推荐 PR Summary：

```text
Switch the default runtime to LangChain create_agent on LangGraph, with one agent selecting structured tools.
Expose three patient information layers alongside IReTour, IReGo, hospital, scene and knowledge tools.
Preserve trusted identity injection, scoped Evidence, bounded context, call budgets and failure degradation.
Keep ORCHESTRATION_MODE=legacy as a fallback and retain the production startup gates.
Sync origin/master and validate 138 passed / 1 environment-dependent skip, with both Ruff checks passing.
Document current limitations; exclude Dify RAG, medical quality evaluation, scaling, new UI and new tools.
```
