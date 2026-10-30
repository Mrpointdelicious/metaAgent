# agent 功能选择性整合与验收报告

日期：2026-10-30（Asia/Shanghai）。目标：`Mrpointdelicious/metaAgent` / `main`。

本轮采用用户最新确认的范围：知识检索以 main 现有 MaxKB-RAG / search_knowledge 为准，
只增加 SingleAgentRuntime 与工具过滤中间件中的提问/动作路由，不新增问答工具、独立子 Agent 或额外 Flow。
场景目录、解析及版本校验在 AI_WebApi 中实现；其余业务能力按原任务保留兼容与安全边界。

## 1. 基线和实施

开始及交付前均执行了 `git fetch origin --prune`。

| 引用 | 实际 SHA |
|---|---|
| origin/main | `81bdd47742284fada6e14a76a15481e3f0c60479` |
| origin/agent | `da08fddcdb3bbc5bb7d09a51a226e83ece2151c5` |
| merge-base | `7249dce78ba4943ca088ee2f6470390288a40423` |

执行并比较了三点差异，以及共同祖先到 main、agent 的两组变化；agent 单侧变化为 32 文件、
4490 行新增、238 行删除。交叉修改覆盖运行时、工具注册、API、应用上下文、知识等，未直接覆盖关键文件。
原工作区处于 `codex/dify-demo`，存在未提交工作；未清理、reset、覆盖或 stash。
独立工作区从最新 origin/main 建立 `integration/agent-features-v2`，没有整支 merge agent。

## 2. 功能迁移清单

下表路径均相对仓库根目录。

| 功能 | agent 来源文件 | 目标文件 | 处理方式 | 实现结果 |
|---|---|---|---|---|
| Dify SSE | adapters/dify.py、api/routes.py | adapters/dify.py、events/dify.py、api/routes.py | 保留 main 投影，增加可选生命周期 | workflow/node 事件，最终答案仅发送一次；多次引用同一旧事件的修订不会重复拼接 |
| 会话历史 HTTP | api/routes.py | api/history_routes.py、infrastructure/repository.py、application/ingress.py | 重写授权查询 | GET /v1/conversations、/v1/messages；必须验证业务主体，不能只凭 user；分页、TTL 和接入方隔离 |
| 单 Agent 业务路由 | agent/runtime.py、agent/context.py、domains/scene_instruction.py 的意图判定 | domains/routing.py、agent/runtime.py、agent/tools.py | 仅迁移路由职责 | 提问与明确动作分流；患者、历史、混合请求分别处理；不增加模型调用或 Flow |
| 场景指令 | domains/scene_instruction.py | domains/scene_instruction.py、domains/scene.py | 分离解析、解析服务调用、交付 | 复用 AI_WebApi commands[]；验证原话覆盖、顺序、空间、版本、Evidence、action_ref 和幂等 |
| 医院目录 | domains/hospital.py | domains/hospital.py、tools/ai_webapi.py | 复用已存在固定 GET API | 名称过滤；机构编号必须来自目录；保留 operator 权限与既有运营分析、报告处理 |
| 医生 | domains/doctors.py | domains/doctors.py、agent/business_tools.py | 保留 main 查询，拆分只读选择 | search_doctors 兼容入口、search_online_doctors 别名、resolve_doctor_selection；不迁移直连登录或手机号挂号命令 |
| 挂号 | domains/doctors.py | agent/business_tools.py | 明确外部依赖 | request_registration 返回 registration_authorization_unavailable，不伪造成功或发起未授权写操作 |
| 知识检索 | domains/company_kb.py、domains/remote_knowledge.py | main 现有 search_knowledge / integrations/maxkb_native.py | 不迁入重复问答 | 沿用 protocol=maxkb 及发布目录、范围、Evidence、引用与预算；已配置来源保持兼容，不新增公司问答工具或配置 |
| 历史会话工具 | domains/chat_history.py | domains/chat_history.py | 重写 | 当前可信用户、当前会话 Repository 查询；无模型可控用户、URL 或跨会话参数 |
| 多接入方和工具限制 | api/security.py、agent/tools.py | api/security.py、orchestration/identity.py、tools/policy.py、execution/dispatcher.py | 适配 main 身份 | 两个服务凭证；权限拒绝贯穿模型工具、执行入口、兼容别名和旧调度路径；次接入方历史独立 |
| 身份方案 A | 旧身份/登录处理 | application/ingress.py | 只建立可信接入边界 | BusinessIdentityProvider 需真实业务会话验证；未移除当前患者凭证，不恢复 robotdb、手机号或旧患者 ID 映射 |
| 子 Agent / 额外 Flow | agent/runtime.py 的 sub_agent / execute_sub、domains/routing.py 的分支设计 | 不迁入 | 按最新用户指示取消 | 保留一个 SingleAgentRuntime 和原有模型/工具预算、超时、取消机制 |
| 安全清理 | .env、.env.local、直连登录常量 | 配置示例、memory/redaction.py、app.py、agent/context.py | 不复制凭证，停止跟踪环境文件 | 本地环境文件保留；请求校验错误不回显输入；长期消息清理凭证、手机号、患者姓名字段及授权链接 |

表中模块前缀如 `domains/`、`agent/` 对应 `src/meta_agent/` 下的目录。

## 3. 架构变化与兼容性

修改前：main 的单 Agent 通过患者工具、知识源、医生/医院适配器和 SceneAdapter 调用后端；
agent 另外维护了知识/场景路由、直接场景 SQL、业务登录和独立 HTTP/LLM 路径。

修改后：

```text
Native / Dify → 原有 ApplicationService → SingleAgentRuntime
    → PatientContextMiddleware（原有患者上下文与预算 + 业务路由提示）
    → available_tools / tool_allowed（权限交集 + 路由收窄）
    → 原有患者 v2 工具 / search_knowledge（现有 MaxKB-RAG）/ 医生医院工具
    → SceneIntentParser → SceneCommandResolver → AI_WebApi.navigate_scene
    → 已验证 commands[] → SceneCommandExecutor → 原有 action_ready
```

MetaAgent 负责业务分流、可信范围、原话覆盖校验、Evidence、超时取消、预算、报告来源绑定和动作交付。
AI_WebApi 负责目录读取、四类目录查询、实时版本、真实命令编号、动作分段、同义词、拼音和歧义判定。
Python 分句仅供路由提示使用；最终动作边界取自 AI_WebApi，未复制匹配打分算法或 SQL 模板。
没有删除 main 的患者模块；agent 的硬编码登录、场景 SQL、独立公司知识 LLM、旧患者映射均未迁入。
业务工具注册集中在 `agent/business_tools.py`，新增业务不必持续扩张患者工具实现。

知识问答是检索后在原模型循环中组织回答的业务行为；MaxKB-RAG 是该行为使用的检索来源。
二者职责分层，当前没有 search_company_knowledge、公司专用 Adapter 或独立 LLM。

原患者 `contract_version=2.0.0`、patient_ref、scope_fingerprint、data_scope、机构归属校验、
expected_source_snapshot 和报告下载授权保留；服务 Bearer 只识别接入服务，不构成患者授权。
用户输入不能覆盖 BusinessIdentityProvider 返回的主体、角色、机构、空间或患者授权；旧患者 ID 在入口拒绝。
没有登录提供方时，既有聊天仍遵守 main 的受信网关接入约定，新历史 HTTP 接口关闭并返回 503。
部署时不能把服务凭证交给不可信前端，让终端用户自行填充受信网关字段。

知识提问（如“个人中心是什么”）不暴露场景工具；明确动作（如“打开个人中心”）使用场景工具；
“去二楼然后查训练报告”和“打开个人中心然后介绍训练页面”仍在同一模型循环处理。
路由永远不能授予患者/运营权限。患者身份缺失或查询失败不影响独立的知识、医生和场景能力。

### Dify

保留 POST /v1/chat 的 Native/Dify 判别、两条 chat-messages 入口及原状态/取消接口。
`META_AGENT__DIFY_WORKFLOW_EVENTS_ENABLED=false` 保持 main 默认帧格式；启用后为：

```text
workflow_started → node_started* → node_finished* → message/error → workflow_finished → message_end
```

并行工具的 node_started 按实际 progress 产生；node_finished 等最终 TaskResult 确认后发送，
不会用“下一个工具开始”猜测上一个结束。节点事件只投影状态、编号和 reason code，不回显输入或原始工具结果。
partial 映射为 Dify succeeded、cancelled 映射为 stopped，原始业务状态另保留在 outcome / metadata.status。
阻塞和流式使用同一个 DifyEventAdapter 的最终答案投影；断线继续取消原任务。

### 场景状态

| 阶段 | 现有协议 | 解释 |
|---|---|---|
| 匹配 | matched / ambiguous / unmatched | 仅匹配结果；同分由后端返回 ambiguous，不取第一个 |
| 待交付/未知 | action_ready + delivery_unknown | 已登记动作，客户端执行结果未知；覆盖 delivery_pending / delivery_unknown 的业务含义 |
| 交给 Native 通道 | dispatched | 仅通道发送；不能映射为 Unity delivered |
| 本地交付失败 | TaskResult / dispatches 中失败状态与 delivery_failed reason code | 不生成执行成功事实，保留原协议枚举 |
| 前端实际执行完成或失败 | 当前缺少独立执行确认契约 | 不伪造 delivered / delivery_failed 的 Unity 回执 |

每个动作引用有独立稳定的 action_id；重复提交同一 action_ref 不重发。
原有 Telepor / ScenePoint / GamePoint 使场景上下文失效的规则保留：
例如“去二楼然后打开训练页面”，第一步交付后需更新场景再继续，第二步不能沿用旧版本执行。
解析预览工具 resolve_scene_instruction 不触发交付，navigate_scene 才进入交付路径。

AI_WebApi 独立分支 codex/scene-command-integration 从 origin/main（57a423b）建立，
选择性移植本地 4c23a1b 的场景模块，没有合入整个患者 v2 分支。
同一路由新增可选 expectedSceneVersion；冲突返回 HTTP 409，data 为空。
Python 发送可信场景版本并保留返回版本复核，兼容尚未支持此可选字段的 commands[] 部署。
外层 status/message/data 封套不变；data 新增 spaceId/version/commands[]，保留 result/intentType/matchName/score。
旧字段仅在恰好一条 matched 时填充；多动作、歧义及无匹配返回空 result，不误取第一条。
患者、报告控制器及其 DTO 未修改，Native/Dify 原有外层报文保留。
本地患者 v2 分支与原工作区未改动；部署需确认患者 v2 与场景分支的合并次序。

### 新配置与历史接入

`.env.example` 提供以下默认关闭/空配置：

- DIFY_WORKFLOW_EVENTS_ENABLED、SERVICE_BEARER_TOKEN_SECONDARY、SECONDARY_EXCLUDED_TOOLS。
- AI_WEBAPI_HOSPITAL_BEARER_TOKEN：医院调用可使用独立服务凭证，未配置时沿用 AI_WEBAPI_BEARER_TOKEN。

均使用 `META_AGENT__` 前缀。真实 `.env`、`.env.local` 保持忽略且本地文件存在。

历史 HTTP 需要部署端安装 `app.state.business_identity_provider`，其契约见
`src/meta_agent/application/ingress.py`：`authenticate(request, client) -> TrustedScope`。
实现必须验证真实业务登录态，并服务端校验角色、租户、空间与数据授权；不能直接信任新增请求头或 user 参数。
患者 scope 仍经过现有 AI_WebApi v2 身份绑定。没有可验证的业务登录 API 时，不提供伪生产适配器。
历史仅投影对话 query/answer，不暴露工具 JSON、身份凭证、动作重放或报告授权链接；过期记录不返回。

## 4. 实际测试报告

| 阶段 | 实际执行命令 | 通过 | 失败 | 跳过 | 环境 |
|---|---|---:|---:|---:|---|
| main 基线 | `uv run --extra dev pytest --ignore=tests/integration -ra` | 273 | 0 | 1 | 合成患者/内存仓库 |
| main 基线静态检查 | `uv run ruff check src tests interactive.py` | 检查通过 | 0 | 0 | 本地 |
| 最终 Python 回归 | `uv run pytest --ignore=tests/integration -ra` | 329 | 0 | 1 | 合成业务数据；含原有真实本地 HTTP 脚本测试 |
| 最终静态检查 | `uv run ruff check src tests interactive.py` | 检查通过 | 0 | 0 | 本地 |
| 新增跨进程场景链路 | 设置 META_AGENT_TEST_SCENE_WEBAPI_ROOT 为本地 AI_WebApi-scene-integration 后执行 `uv run pytest tests/integration/test_scene_webapi_offline.py -ra` | 6 | 0 | 0 | 真实 .NET 场景集成分支 + 合成目录上游；脚本模型 |
| AI_WebApi main 基线 | 设置 DOTNET_ROLL_FORWARD=LatestMajor 后 `dotnet test AI_WebApi.Tests/AI_WebApi.Tests.csproj --no-restore --verbosity quiet` | 162 | 0 | 1 | 未配置独立只读项目数据库 |
| AI_WebApi 场景集成 | 同上 | 320 | 0 | 1 | 原有回归 + 158 项场景、版本及兼容测试 |
| AI_WebApi 构建 | `dotnet build AI_WebApi/AI_WebApi.csproj --verbosity quiet` | 构建成功 | 0 错误 | 0 | 9 项已有框架/依赖兼容告警 |
| AI_WebApi 实际 HTTP | `python tools/verify_scene_command_resolver.py` | 验证通过 | 0 | 0 | 多动作、旧字段、400/401/409/502、OpenAPI |

Python 的 1 项跳过来自未配置独立 `META_AGENT_TEST_POSTGRES_DSN`；没有删测试或降低断言。
开发过程首轮曾出现 2 项失败：合成 Backend 尚无目录能力、新增 Python 文件缺少仓库要求的文件头；
已补齐合成目录夹具及文件头，最终无新增失败。其余开发过程静态问题已修复。
版本冲突跨进程初测曾有1项断言期望Native公开内部工具原因码；改为检查可信运行记录及无动作输出后6项均通过，未更改报文语义。

新增 56 项非 integration 测试，覆盖：知识/动作/患者/历史/否定/混合意图，真实 create_agent 循环，
工具可见性与执行拒绝、知识来源引用/预算/身份隔离、医生选择与挂号分离、目录过滤和伪造编号、
历史主体与接入方隔离、分页/TTL、错误脱敏、答案修订及 SSE 断线取消。
场景包含去二楼、打开个人中心、切换视角、回到入口、去上一层、两动作顺序、目录为空、点位不存在、
同分歧义、版本变化、原话伪造、重复交付、Teleport 后失效，以及匹配成功但交付失败。

6 项跨进程测试验证 Native blocking、Dify blocking、Dify streaming 的正常和版本冲突分支：
MetaAgent → 真实 AI_WebApi HTTP → 本地合成目录，确认返回真实目录编号、两个动作顺序与独立幂等标识，
Dify 生命周期顺序、消费事件后仍是 delivery_unknown，及 409 不产生动作。
.NET HTTP 测试通过 LatestMajor 使用本机兼容运行时；不等于生产 .NET 6 环境验收。

未运行：真实 Dify WebApp/服务、真实医生/医院/知识服务、真实业务登录/挂号、真实患者 HTTP 联调、
PostgreSQL、Unity 执行回执、生产发布。未使用真实患者数据或生产写操作。
既有患者 live 测试需要单独的 META_AGENT_LIVE_PATIENT_V2_CONFIG，本轮没有加载。

## 5. 安全发现与外部依赖

### 当前上线前必须处理

历史中存在环境配置和 agent 医生/场景模块中的凭证常量。
没有测试这些凭证是否仍有效；应视为已披露，由凭证所有者轮换并撤销旧凭证，包括：
MetaAgent 服务凭证、AI_WebApi 服务凭证、场景 API 凭证、业务登录账户密码和相关数据库凭证。
报告不记录秘密值。停止跟踪当前文件不会清除 Git 历史，也不能代替轮换。
没有自动改写历史或修改运行中的账户；需由所有者审计历史、撤销授权和检查访问记录。

本轮代码及本地合成链路满足收窄后的单 Agent 验证范围，不能宣称整个生产验收完成。
在外部授权、凭证轮换和部署链路确认前维持 draft，不自动合并或发布。

### 需要业务登录系统支持

需要可验证登录态的 API / 服务适配器，确认用户、角色、租户、空间和患者范围授权。
这是开放历史 HTTP 接口及后续移除 identity_context_token 的前置依赖。
本轮保留当前患者 Token、服务间 Bearer 和报告下载授权各自职责，不恢复旧患者映射。

### 需要 AI_WebApi / 前端协同的独立任务

1. 医生挂号授权：现有 search_doctors 只读接口不能证明挂号成功；需独立、授权、幂等的挂号 API，
   输入来自受信医生目录的 opaque doctor_ref、经验证业务主体和 idempotency_key，输出 request_ref 及明确受理状态。
   禁止手机号命令代替服务端授权。这里仅提出依赖契约，没有新增调用不存在的端点。
2. 量表/训练导航、病历/报告页面导航：需明确授权的业务导航契约及当前目录映射；不能复制 agent 的固定楼层/登录账户。
   患者查询和报告生成继续使用已有 v2 工具，不塞进 SceneInstructionAdapter。
3. Unity 动作回执：需独立确认 action_id、space/version、执行状态及幂等；transport dispatched 不能作为 executed。
4. 场景“那个/上一个目标”等上下文指代目前服务端未支持；保持 unmatched/澄清，不能在 Python 猜命令 ID。
   同义词、拼音、同分候选和多动作已选择性移入 AI_WebApi 独立 PR，无需另建目录 API。

### 可延后优化

历史列表当前在严格限定的 scope 命名空间内分页扫描后排序；大体量历史可另行建立索引。
规则路由是保守提示，可随真实业务语料扩展；默认不增加模型调用。
独立子 Agent / Flow 按用户最新指示不属于本轮范围，未留下半启用接口。

## 6. Git 交付与合并判断

分支：`integration/agent-features-v2`，基于上述最新 origin/main。
最终提交 SHA 与 draft PR 地址以交付消息及 PR head 为准；未修改 agent/main、未自动合并或发布。
实际修改文件清单如下（提交前生成，包含本报告）。

- `.env`
- `.env.example`
- `.env.local`
- `README.md`
- `docs/integration-agent-features-v2.md`
- `src/meta_agent/adapters/dify.py`
- `src/meta_agent/agent/business_tools.py`
- `src/meta_agent/agent/context.py`
- `src/meta_agent/agent/runtime.py`
- `src/meta_agent/agent/tools.py`
- `src/meta_agent/api/history_routes.py`
- `src/meta_agent/api/routes.py`
- `src/meta_agent/api/security.py`
- `src/meta_agent/app.py`
- `src/meta_agent/application/context.py`
- `src/meta_agent/application/ingress.py`
- `src/meta_agent/application/service.py`
- `src/meta_agent/config.py`
- `src/meta_agent/domains/chat_history.py`
- `src/meta_agent/domains/doctors.py`
- `src/meta_agent/domains/hospital.py`
- `src/meta_agent/domains/routing.py`
- `src/meta_agent/domains/scene.py`
- `src/meta_agent/domains/scene_instruction.py`
- `src/meta_agent/events/dify.py`
- `src/meta_agent/execution/dispatcher.py`
- `src/meta_agent/infrastructure/repository.py`
- `src/meta_agent/memory/redaction.py`
- `src/meta_agent/orchestration/identity.py`
- `src/meta_agent/tools/ai_webapi.py`
- `src/meta_agent/tools/policy.py`
- `tests/helpers/integration.py`
- `tests/helpers/runtime.py`
- `tests/integration/test_scene_webapi_offline.py`
- `tests/test_business_tools_integration.py`
- `tests/test_integration_features_api.py`
- `tests/test_scene_instruction_integration.py`
- `tests/test_single_agent_business_routing.py`

合并判断：收窄后的单 Agent 功能迁移已通过自动化及本地场景 HTTP 验证，可进行代码审查；
原任务的完整生产验收尚未满足，凭证轮换、真实授权与外部业务/前端联调仍需独立完成。

## 7. AI_WebApi 独立改动清单

下列文件仅在 AI_WebApi 场景集成分支修改，不包含患者/报告业务变更：

- `.env.ai_webapi.example`
- `AI_WebApi.Model/AITools/RehabToolsOptions.cs`
- `AI_WebApi.Model/DTOs/AITools/RehabToolDtos.cs`
- `AI_WebApi.Services/AITools/Rehab/SceneCommandCatalog.cs`
- `AI_WebApi.Services/AITools/Rehab/SceneCommandNormalizer.cs`
- `AI_WebApi.Services/AITools/Rehab/SceneCommandResolver.cs`
- `AI_WebApi.Services/AITools/Rehab/SceneNavigationService.cs`
- `AI_WebApi.Tests/AI_WebApi.Tests.csproj`
- `AI_WebApi.Tests/SceneCommandResolverTests.cs`
- `AI_WebApi.Tests/SceneHttpBoundaryTests.cs`
- `AI_WebApi/Controllers/AiRehabToolsController.cs`
- `AI_WebApi/Swagger/DifyToolSecurityOperationFilter.cs`
- `docs/scene-command-resolver.md`
- `tools/verify_scene_command_resolver.py`
