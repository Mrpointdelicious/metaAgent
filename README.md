# MetaAgent Rehab Review Demo

面向康复训练师的计划执行偏离识别与复核支持系统 Demo。

本项目不是通用医疗聊天系统，也不是纯 RAG 系统。当前主目标是围绕院内康复训练流程，帮助训练师识别和复核：

- 哪些患者最近出现计划执行偏离
- 偏离主要体现在哪些维度
- 偏离是否伴随结果变化
- 哪些患者值得优先人工复核
- 哪些开放式集合分析问题可以由受控工具链回答

## 当前标准主链

当前真实运行主链已经收口为一条明确的编排链路：

```text
用户输入
-> request normalization
-> IntentRouter 规则路由
-> LLMRouter 按需精修 intent / subtype / scope
-> choose_execution_strategy
-> 三种执行策略之一
   -> fixed_workflow
   -> template_analytics
   -> agent_planned
-> shared response / trace
```

`direct` 和 `agents_sdk` 现在只是运行模式，不再是项目的主架构分叉。主链的唯一策略裁决点是 `ExecutionStrategy`。

## 执行策略

### `fixed_workflow`

用于高频且口径稳定的固定任务：

- 单患者复核
- 多患者风险筛选
- 周报 / 风险摘要
- 预留步态专项复核入口

固定流程仍然是主路径和回归基线，不会被 LLM Planner 覆盖。

### `template_analytics`

用于标准开放分析模板。当前已支持：

- `absent_old_patients_recent_window`
- `absent_from_baseline_window`
- `doctors_with_active_plans`

模板分析是开放分析的稳定兜底路径。

### `agent_planned`

用于更灵活的开放分析。触发后由：

1. `LLMPlanner` 在工具白名单内生成结构化 `QueryPlan`
2. `PlanValidator` 做白名单、参数、scope、步数和 SQL-like 文本校验
3. `AnalyticsManager` 使用统一执行器执行合法计划
4. 任一环节失败则 fallback 到模板分析

LLM 只负责规划，不直接访问数据库、不生成 SQL、不调用 repository。

## 运行模式

`agent/orchestrator.py` 会先解析 LLM 配置和运行模式：

- 如果 provider 凭据、模型和 base URL 可用，默认可进入 `agents_sdk`
- 如果显式关闭 SDK 或凭据不可用，则进入 `direct` / `direct_fallback`
- `agent_planned` 只有在策略为 `agent_planned` 且运行模式可用时才会真正调用 planner
- 固定 workflow 和模板 analytics 都可以在 `direct` 下稳定运行

因此运行模式只影响“是否能调用 LLM Router / Planner”，不改变主链的策略结构。

## 架构分层

```text
Demo/              命令行与交互式入口
agent/             编排层、路由、LLM Router、LLM Planner、Plan Validator
config/            配置读取与环境变量
models/            Pydantic 数据结构
repositories/      只读数据访问层
services/          业务逻辑层
tools/             service 工具包装层
tests/             回归测试
```

### 输入层

- `Demo/cli.py`：单次命令入口
- `Demo/main.py`：常驻交互入口
- `Demo/dialogue.py`：自然语言解析、上下文续接、命令容错

### 编排层

- `agent/orchestrator.py`
  - runtime assembly
  - resolve LLM config / execution mode
  - rule route
  - LLM route refine
  - strategy choose
  - fixed workflow dispatch
  - analytics dispatch

- `agent/intent_router.py`
  - 规则路由
  - 输出 `intent / analytics_subtype / analysis_scope / doctor_id_source`

- `agent/llm_router.py`
  - 按需精修 `intent / subtype / scope`
  - 不执行工具

- `agent/analytics_manager.py`
  - 公开入口统一为 `run(..., strategy=...)`
  - 内部再选择模板或 planner 分支
  - 负责开放分析 trace、fallback、统一执行器

- `agent/llm_planner.py`
  - 在工具白名单内生成结构化 `LLMPlannedQuery`

- `agent/plan_validator.py`
  - 校验 LLM 计划是否可执行

### 业务逻辑层

- `services/plan_service.py`
- `services/execution_service.py`
- `services/outcome_service.py`
- `services/gait_service.py`
- `services/deviation_service.py`
- `services/report_service.py`
- `services/reflection_service.py`
- `services/analytics_service.py`

业务判断仍在 service 层，LLM 不承载核心医疗业务口径。

### 数据层

- `repositories/rehab_repository.py`：业务查询封装
- `repositories/db_client.py`：只读 MySQL 客户端
- `repositories/mock_data.py`：数据库不可用时的 mock fallback

数据库访问默认只读。

## A/B 产品链边界

### A 链：下肢康复机器人产品链

A 链是当前主业务范围，承担：

- 计划执行偏离识别
- 单患者复核
- 多患者风险筛选
- 周报 / 风险摘要

核心表：

- `dbtemplates`
- `dbrehaplan`
- `dbdevicelog`
- `dbreport`

核心判断：

- 计划是否存在
- 患者是否到训
- 是否完成
- 实际剂量是否低于计划剂量
- 结果是否下降
- 是否需要优先人工复核

### B 链：康复步道产品链

B 链当前以独立证据块保留，用于步态 / 步道解释扩展。

核心表：

- `dbwalk`
- `walkreport`
- `walkreportdetails`

当前 B 链输出不参与 A 链偏离指标、风险分和周报统计。代码中 `gait_explanation` 是独立证据块，不表示 A/B 链已经合并。

## 开放分析与 Planner

开放分析入口用于处理自然语言统计和集合分析问题。

### 当前支持的 primitive tools

Planner 只看到 analytics primitive tools，不暴露高层固定 workflow 工具。典型工具包括：

- `list_patients_seen_by_doctor`
- `list_patients_with_active_plans`
- `set_diff`
- `get_patient_last_visit`
- `get_patient_plan_status`
- `rank_patients`
- `list_doctors_with_active_plans`

不会暴露给 planner 的高层工具包括：

- `generate_review_card`
- `screen_risk_patients`
- `generate_weekly_risk_report`

### Tool Catalog 暴露字段

传给 LLM 的 catalog 只包含精简元数据：

- `tool_name`
- `description`
- `input_schema`
- `chain_scope`
- `notes`

不会把 Python 实现或 repository 查询逻辑发给模型。

### Plan Validator 校验

`PlanValidator` 至少拦截：

- 非白名单工具
- 超过最大步数的计划
- SQL-like 文本
- scope 不匹配
- doctor aggregate 中错误注入单医生过滤
- single doctor 分析缺少医生 scope
- 排序策略不合法
- 明显重复的无意义步骤
- 工具参数不符合 `ToolSpec.validate_args()`

### Fallback

以下情况会回退到模板分析：

- planner 调用失败
- planner 输出解析失败
- validator 校验失败
- 执行计划时关键步骤失败
- 工具名不在白名单内
- scope 不合法
- 当前运行模式无法安全使用 LLM planning

fallback 会写入 `execution_trace` 和 `structured_output.planned_query_source`。

## 输出结构

所有入口最终返回 `OrchestratorResponse`：

- `success`
- `task_type`
- `execution_mode`
- `llm_provider`
- `llm_model`
- `structured_output`
- `final_text`
- `validation_issues`
- `execution_trace`

开放分析结果会额外标记：

- `planned_query_source.source`
  - `fixed_template`
  - `llm_planner`
  - `fallback_template`

## 环境配置

推荐使用 Python 3.11+。

安装：

```powershell
python -m pip install -e .
```

`.env` 示例：

```env
LLM_PROVIDER=qwen
AGENTS_TRACING_ENABLED=false

OPENAI_API_KEY=
OPENAI_MODEL=
OPENAI_BASE_URL=

QWEN_API_KEY=
QWEN_MODEL=qwen-plus
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com

MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=meta_universe
MYSQL_USER=meta_user
MYSQL_PASSWORD=
USE_MOCK_WHEN_DB_UNAVAILABLE=true
```

数据库不可用时，如果 `USE_MOCK_WHEN_DB_UNAVAILABLE=true`，系统会使用 mock fallback。

## 启动方式

### 交互模式

```powershell
python Demo\main.py
```

交互命令：

```text
review-patient --plan-id 6 --days 30
screen-risk --therapist-id 56 --days 30
weekly-report --therapist-id 56 --days 30
帮我复核计划 6
看一下医生 56 最近 30 天的高风险患者
给我这个医生最近 7 天的周报
换成最近 7 天
```

运行时切换：

```text
set-provider qwen
set-model qwen-plus
set-base-url https://dashscope.aliyuncs.com/compatible-mode/v1
set-agent on
set-agent off
set-trace on
show-llm
clear-llm
```

### 单次命令

```powershell
python Demo\cli.py review-patient --plan-id 6 --days 30
python Demo\cli.py screen-risk --therapist-id 56 --days 30
python Demo\cli.py weekly-report --therapist-id 56 --days 30
python Demo\cli.py ask "查看医生56这30天有哪些以前来过的患者没有来"
python Demo\cli.py --use-agent-sdk ask "看医生56这30天有哪些是前80-30天以前来过的患者，这30没有来"
python Demo\cli.py --json --show-trace ask "查询一下这30天哪些医生有定患者训练计划？"
```

## 稳定 Demo 样本

当前建议用于演示和回归的样本：

- `therapist_id=56`
- `plan_id=6`
- `patient_id=146`

## 测试

```powershell
python -m py_compile agent\orchestrator.py agent\analytics_manager.py agent\schemas.py tests\test_open_analytics.py
python -m unittest discover -s tests -v
```

当前开放分析测试覆盖：

- 固定任务不触发 LLM Router
- 标准模板问题走 template analytics
- 双窗口问题可走 agent planned
- 医生聚合问题不注入单医生过滤
- planner 生成非法工具时由 validator 拦截并 fallback

## 当前边界

- 风险评分仍是规则版，不是学习版
- `dbdevicelog.PlanId` 需要逻辑清洗，不能直接当严格 session 主键
- `dbrehaplan.StartTime` 不作为精确计划开始时间
- A 链结果层仍以结构化抽取为主，尚未深入 `dbreportdata`
- B 链尚未形成完整的独立步道复核入口
- 医生聚合和复杂统计类 planner 能力仍依赖现有 analytics primitive tools 的覆盖度

## 一句话结论

MetaAgent 当前已经收口为：固定 workflow + template analytics + agent planned 三策略主链。LLM Router 和 LLM Planner 被限制在识别与规划层，所有执行仍由代码侧工具白名单、校验器、service 和 repository 完成。
