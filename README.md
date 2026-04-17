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
