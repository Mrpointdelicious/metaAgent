# MetaAgent Rehab Review

MetaAgent 是面向康复训练师与患者会话的服务端编排器。当前系统不是通用医疗聊天系统，也不是纯 RAG 系统，而是围绕院内康复训练流程，提供计划执行偏离识别、复核支持、开放分析与轻量实体查询。

## 正式入口

当前正式入口已经从 Demo 层收口到服务端与核心编排层：

- `server/main.py`：生产/接口入口，从 JSON payload 构造正式请求并调用 orchestrator。
- `server/request_factory.py`：统一 request normalization 与 `OrchestratorRequest` 构造入口。
- `server/session_context.py`：构造权威的 `SessionIdentityContext`。
- `Demo/doctor_demo.py`：医生身份演示入口，只注入显式 `doctor_id`。
- `Demo/patient_demo.py`：患者身份演示入口，只注入显式 `patient_id`。
- `Demo/main.py`：legacy/local debug shell，不是正式服务入口；使用时也必须显式传入医生或患者身份。

Demo 只做适配与演示，不承担正式问题路由、身份判定、权限来源或生产兜底。

## 运行时主链

正式主链为：

```text
外部请求 / Demo 输入
-> request normalization
-> SessionIdentityContext 注入
-> IntentRouter 规则路由
-> LLMRouter 按需 refine intent / subtype / scope
-> choose_execution_strategy
-> fixed_workflow / template_analytics / agent_planned
-> shared response / trace
```

Analytics 域内部继续独占：

```text
agents_sdk_runtime -> llm_planner -> template fallback
```

`IntentRouter` 是唯一正式规则路由器。`LLMRouter` 只在低置信、模糊问题、开放分析细分等场景做 refine，不是主要分类器。

## 身份与权限

服务端请求必须携带身份上下文来源字段：

- 只传 `doctor_id`：当前主体为医生。
- 只传 `patient_id`：当前主体为患者。
- 同时传 `doctor_id` 和 `patient_id`：当前主体为医生，`patient_id` 作为目标患者。
- 两者都不传：请求被拒绝，不进入业务编排。

主链字段优先级固定为：

1. `SessionIdentityContext`
2. request 显式参数
3. 文本中的目标对象线索
4. 普通会话上下文
5. 禁止使用 Demo 默认 doctor/patient 作为生产兜底

权限边界在代码层执行，不依赖 prompt 或 Agent 自律。医生默认只能访问自己职责范围内的患者；患者默认只能访问自己的信息，不能进入多患者筛选或医生聚合。

## IntentRouter 能力

`agent/intent_router.py` 当前承担正式问题路由：

- 固定 workflow：单患者复核、风险筛选、周报/风险摘要。
- 开放分析：患者集合、双窗口分析、医生聚合等 subtype/scope 初判。
- lookup/entity query：如“查询医生59的名字”“患者146叫什么”“59是谁”。
- 保守判定：只含 ID 或低置信问题不会被硬压到高频固定任务，而是保留为低置信开放分析或交给 LLM refine。

lookup 查询不会暴露 `dbuser` 表给 Agent；姓名查询由 repository/service 层完成。

### 身份感知用户查询工具

当前新增 3 个窄工具，供 direct 路径与 Agents SDK runtime 复用：

- `lookup_accessible_user_name(user_id)`：只在当前会话身份可访问该用户时返回姓名。
- `list_my_patients(days=None)`：医生会话使用，列出与当前医生接诊过或存在训练计划关系的患者。
- `list_my_doctors(days=None)`：患者会话使用，列出与当前患者接诊过或存在训练计划关系的医生。

权限控制不依赖 prompt。`services/user_lookup_service.py` 负责权威授权判断，`repositories/rehab_repository.py` 负责用计划记录与执行日志判定“相关”关系，并通过批量 `dbuser` 查询补齐名称。

Agent 可见工具白名单会按身份裁剪：

- 医生会话：可见 `lookup_accessible_user_name`、`list_my_patients`，不可见 `list_my_doctors`。
- 患者会话：可见 `lookup_accessible_user_name`、`list_my_doctors`，不可见 `list_my_patients`。

因此 Agent 不能自行决定权限范围，也不会获得直接查询 `dbuser` 或万能 related-user 查询工具。

## 执行策略

`choose_execution_strategy` 是顶层策略裁决点：

- `fixed_workflow`：高频稳定任务。
- `template_analytics`：标准开放分析模板。
- `agent_planned`：SDK 与 LLM 配置可用时的复杂开放分析路径。

当 LLM/Agents SDK 配置不可用时，系统回退到 direct/template 路径，并在 trace/validation issues 中标明原因。

## 输出结构

所有入口最终返回 `OrchestratorResponse`：

- `success`
- `task_type`
- `execution_mode`
- `structured_output`
- `final_text`
- `validation_issues`
- `execution_trace`

开放分析的 `structured_output.planned_query_source.source` 可能为：

- `fixed_template`
- `agents_sdk_runtime`
- `llm_planner`
- `fallback_template`

## 运行示例

生产入口从 stdin 读取 JSON：

```bash
python server/main.py
```

示例 payload：

```json
{
  "doctor_id": 30001,
  "question": "看一下最近7天高风险患者",
  "days": 7
}
```

医生演示：

```bash
python Demo/doctor_demo.py --doctor-id 30001 --question "查询医生30001的名字"
```

患者演示：

```bash
python Demo/patient_demo.py --patient-id 20001 --question "我最近的训练情况怎么样"
```

legacy debug shell：

```bash
python Demo/main.py
```

该入口仅用于本地调试，不代表生产主链。

## 目录概览

```text
agent/        编排、路由、LLM refine、planner、validator、analytics manager
server/       服务端入口、request factory、session identity 构造
Demo/         显式身份演示入口与 legacy debug shell
models/       Pydantic 结构
repositories/ 只读数据访问与 mock fallback
services/     业务逻辑与权限作用域执行
tools/        受控工具包装
tests/        主链、开放分析、身份与路由测试
```

## Session 原始历史记忆

当前已经启用 OpenAI Agents SDK session 作为第一层原始历史记忆。以前 `server/request_factory.py` 只把 `session_id` / `conversation_id` 写入 `SessionIdentityContext`，但 `agent/open_analytics_agent.py` 调用 `Runner.run_sync(...)` 时没有传入 `session`，所以 SDK 不会自动读取或写入多轮原始历史；多轮只靠入口继续传 `doctor_id` / `patient_id`，不能保证 follow-up 复用上一轮上下文。

现在的链路是：

```text
frontend payload
-> server.request_factory.ensure_session_ids
-> SessionIdentityContext.session_id / conversation_id
-> OpenAnalyticsAgentRuntime._session_for_request
-> AgentSessionManager.get_or_create_session(session_id)
-> Runner.run_sync(..., session=agent_session)
```

`session_id` 是 SDK 原始历史的唯一主键：相同 `session_id` 复用同一段 raw transcript，不同 `session_id` 互相隔离。`conversation_id` 是业务追踪字段，可以用于日志、前端会话或后续 thread state 关联，但不会把不同 `session_id` 的 SDK 历史合并。

正式入口 payload 支持并建议显式传入：

```json
{
  "doctor_id": 56,
  "session_id": "s1",
  "conversation_id": "c1",
  "question": "查询我所有的患者"
}
```

同一个前端会话的后续请求必须继续携带同一个 `session_id` / `conversation_id`。如果 payload 缺少其中任一字段，`server/request_factory.py` 会用固定规则生成 `sess_<uuid>` / `conv_<uuid>`，`server/main.py` 会在响应里返回最终使用的值，调用方应保存并在下一轮继续传回。

Session 存储由 `server/session_manager.py` 统一管理。生产配置优先使用 OpenAI Agents SDK 官方 `RedisSession`：

```text
AGENT_SESSION_BACKEND=redis
AGENT_SESSION_REDIS_URL=redis://127.0.0.1:6379/0
AGENT_SESSION_REDIS_KEY_PREFIX=metaagent:agents:session
AGENT_SESSION_TTL_SECONDS=86400
```

Redis 适合当前场景，因为业务主数据仍在 MySQL，而会话原始历史是短期状态，需要低延迟读写、TTL、隔离和快速清理。`memory` backend 只用于单进程测试和本地调试，不作为生产会话存储。

本轮只落地第一层 raw history。后续多层上下文会继续扩展：

- raw transcript：由 SDK session / Redis session 保存完整原始消息历史。
- working memory：窗口化摘要，压缩最近多轮重点，减少 prompt 压力。
- thread state store：保存结构化状态，例如当前患者集合、筛选条件、默认时间窗。
- result artifacts：大结果集只存引用或 artifact id，不把 120 名患者等大对象长期塞进 prompt。
