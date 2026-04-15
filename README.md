# MetaAgent Rehab Review Demo

面向康复训练师的计划执行偏离识别与复核支持系统 demo。

当前目标不是通用医疗聊天，也不是纯 RAG，而是围绕院内康复训练流程，帮助治疗师快速回答这些问题：

- 哪些患者最近出现了计划执行偏离
- 偏离主要体现在哪些维度
- 偏离是否伴随结果变化
- 哪些患者值得优先人工复核

## 业务定位

本项目当前聚焦的是：

- A 链下肢康复机器人产品的计划执行偏离识别
- A 链训练师侧单患者复核、多患者筛选、风险周报
- B 链康复步道产品的数据审计与步态/执行质量解释扩展位

链路边界以数据库审计文档为准：

- [rehab_execution_deviation_data_audit_v2.md](</D:/Project/Docker/mysql/rehab_execution_deviation_data_audit_v2.md>)

## A/B 链业务澄清

先澄清一个核心点：

**A 链和 B 链是两个独立产品链，不是主从关系。**

### A 链

A 链在审计文档中的定义是“下肢康复机器人产品链”。

当前核心表：

- `dbtemplates`
- `dbrehaplan`
- `dbdevicelog`
- `dbreport`

业务问题：

- 某个计划是否存在
- 患者是否到训
- 是否完成
- 实际剂量是否低于计划剂量
- 结果是否下降
- 哪些患者需要训练师优先复核

当前项目里“计划执行偏离识别”这个主任务，指的就是 A 链。

### B 链

B 链在审计文档中的定义是“康复步道产品链”。

当前核心表：

- `dbwalk`
- `walkreport`
- `walkreportdetails`

业务问题：

- 步道训练是否发生
- 步道训练是否完成
- 步道训练时长和状态是否异常
- 步态质量、准确率、完成率等专项指标如何解释

### A 链和 B 链的关系

根据审计文档，A/B 链之间应这样理解：

- 二者是独立链，不共享统一任务口径
- 二者不应在当前阶段进入同一个偏离识别模型
- 二者不应被混成同一个“总 session”
- B 链不是 A 链的解释增强从链，也不是 A 链的附属模块

当前 demo 中两条链的实现关系是：

- A 链承担当前主任务的偏离指标计算与风险判断
- B 链在代码中以 `gait_explanation` 独立证据块形式保留，用于扩展步态解释能力
- B 链输出当前**不参与** A 链的偏离指标、风险分和周报统计

也就是说：

**当前代码里虽然会在单患者输出中挂出 B 链解释字段，但这只是独立证据块，不代表 A/B 链被合并成主从结构。**

## 当前可落地任务

当前 demo 已实现三类 A 链任务：

1. 单患者复核
2. 多患者风险筛选
3. 周报 / 风险摘要

当前 B 链在 demo 中的状态是：

- 已有单患者维度的独立解释输出能力
- 尚未单独暴露成“康复步道独立复核入口”

## 当前工作流程

当前系统有两种输入方式，但最后会收敛到同一条编排链路。

### 1. 输入层

结构化输入：

- `Demo/cli.py`

多轮自然语言输入：

- `Demo/main.py`
- `Demo/dialogue.py`

其中 `Demo/main.py` 当前支持：

- 结构化命令
- 自然语言输入
- 多轮上下文续接
- 运行时切换 LLM provider / model / base URL
- 误把 Python 启动命令贴进交互窗口时的容错

### 2. 编排层

统一编排入口：

- `agent/orchestrator.py`

核心职责：

- 识别任务类型
- 组装 service 层依赖
- 决定走 `direct` 还是 `agents_sdk`
- 输出结构化结果和可读摘要

### 3. 执行层

主业务编排由：

- `services/report_service.py`

向下调用：

- `plan_service.py`
- `execution_service.py`
- `outcome_service.py`
- `gait_service.py`
- `deviation_service.py`
- `reflection_service.py`

### 4. 数据层

统一数据访问入口：

- `repositories/rehab_repository.py`

底层只读连接：

- `repositories/db_client.py`

数据库不可用时回退：

- `repositories/mock_data.py`

### 5. 输出层

输出分两层：

- `structured_output`
- `final_text`

其中 `structured_output` 适合程序消费，`final_text` 适合命令行直接阅读。

## 模块调用关系

下面按当前代码的真实调用顺序说明。

### 单患者复核

调用链：

`Demo/main.py / Demo/cli.py`
-> `OrchestratorRequest`
-> `RehabAgentOrchestrator.run`
-> `ReportService.generate_review_card`
-> `PlanService.get_plan_summary`
-> `ExecutionService.get_execution_logs`
-> `OutcomeService.get_outcome_change`
-> `GaitService.get_gait_explanation`
-> `DeviationService.calc_deviation_metrics`
-> `ReflectionService.reflect_on_output`
-> `ReviewCard`

业务解释：

- `PlanService` 负责 A 链计划摘要
- `ExecutionService` 负责 A 链执行日志汇总
- `OutcomeService` 负责 A 链结果变化摘要
- `GaitService` 负责 B 链独立证据块提取
- `DeviationService` 只基于 A 链可用字段计算偏离与风险
- `ReflectionService` 做受约束检查，不做自由反思

### 多患者风险筛选

调用链：

`Demo/main.py / Demo/cli.py`
-> `RehabAgentOrchestrator.run`
-> `ReportService.screen_risk_patients`
-> `ReportService.generate_weekly_risk_report`
-> `RehabRepository.get_plan_records`
-> 对患者逐个调用 `generate_review_card`
-> 汇总为 `PatientRiskSummary[]`

业务解释：

- 先按 `DoctorId -> Patient -> Plan` 取 A 链计划池
- 再对每个患者复用单患者复核逻辑
- 最后按风险分排序，返回优先复核名单

### 周报 / 风险摘要

调用链：

`Demo/main.py / Demo/cli.py`
-> `RehabAgentOrchestrator.run`
-> `ReportService.generate_weekly_risk_report`
-> `build_time_range`
-> `RehabRepository.get_plan_records`
-> 对患者逐个调用 `generate_review_card`
-> 汇总为 `WeeklyRiskReport`

业务解释：

- 周报不是单独的一套分析逻辑
- 它是在“患者级复核结果”之上做聚合
- 因此单患者复核逻辑是整个 demo 的核心复用单元

### Agents SDK 模式

当启用 `Agents SDK` 时，调用链会多一层 tool adapter：

`Demo/main.py / Demo/cli.py`
-> `RehabAgentOrchestrator.run`
-> `tools/*.py`
-> `services/*.py`
-> `repositories/*.py`

当前 tools 只是 service 的适配层，不承载核心业务逻辑。

## 当前实现中的链路边界

结合审计文档和当前代码，边界应这样理解：

- A 链是当前“计划执行偏离识别 + 训练师复核支持”的主业务范围
- B 链是康复步道独立产品链，不并入当前 A 链主模型
- `dbcyclereport` 当前视为并行聚合链，不进入 session 级主分析

当前代码中与该边界一致的部分：

- 偏离指标只按 A 链字段定义
- A 链周报与风险筛选只按 `DoctorId -> Patient -> Plan` 聚合
- B 链不参与风险分计算

当前代码中保留的扩展位：

- 单患者输出结构里保留 `gait_explanation`
- 这便于后续把 B 链独立建设为步道专项复核模块
- 但不应把这个字段理解成“B 链已经并入 A 链”

## 软件架构

项目按“三层 + Demo 入口”组织。

### 1. `repositories/`

只负责读数据库和提供 mock fallback。

- `db_client.py`
  只读 MySQL 客户端
- `rehab_repository.py`
  面向业务的查询封装
- `mock_data.py`
  数据库不可用时的 mock 数据

### 2. `services/`

真正的业务逻辑层，不依赖 Agents SDK。

- `plan_service.py`
  A 链计划层摘要与计划任务解析
- `execution_service.py`
  A 链执行日志汇总
- `outcome_service.py`
  A 链结果变化与报告解析
- `gait_service.py`
  B 链步态/步道独立证据块提取
- `deviation_service.py`
  A 链偏离指标计算
- `report_service.py`
  单患者复核卡、风险筛选、周报聚合
- `reflection_service.py`
  受约束输出检查
- `shared.py`
  时间窗、JSON 解析、通用统计

### 3. `tools/`

把 service 包装成 agent 可调用工具。

当前工具包括：

- `get_plan_summary`
- `get_execution_logs`
- `calc_deviation_metrics`
- `get_outcome_change`
- `get_gait_explanation`
- `generate_review_card`
- `screen_risk_patients`
- `generate_weekly_risk_report`
- `reflect_on_output`

### 4. `agent/`

负责任务编排，不承载核心业务逻辑。

- `schemas.py`
  编排输入输出结构
- `instructions.py`
  agent 指令模板
- `orchestrator.py`
  任务分类、service 组装、tool 调用、provider 切换、direct fallback

### 5. `Demo/`

命令行入口。

- `Demo/main.py`
  常驻 CMD 交互服务入口，支持多轮自然语言
- `Demo/cli.py`
  单次命令入口
- `Demo/dialogue.py`
  自然语言解析、上下文状态、命令容错
- `Demo/README.md`
  Demo 层使用说明

### 6. `models/` 与 `config/`

- `models/`
  Pydantic 数据结构
- `config/settings.py`
  环境变量读取、稳定 demo 样本、LLM provider 配置

## 偏离指标

偏离指标口径以数据库审计文档中的 A 链定义为准。

当前一级指标：

- 到训率
- 完成率
- 执行剂量偏差
- 连续中断风险

当前实现原则：

- 这四项仅针对 A 链定义
- 不直接引入 `dbwalk / walkreport / walkreportdetails`
- `dbdevicelog.PlanId` 不裸连使用，而是结合时间窗与多证据汇总
- `dbdevicelog.IsComplete` 不作为唯一完成标记
- `dbreport.ReportDetails` 作为结果层重要来源，但先做结构化抽取

## 多厂商 LLM 支持

当前已支持：

- `openai`
- `qwen`
- `deepseek`

默认 provider：

- `qwen`

配置入口在 `.env`：

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
```

说明：

- `LLM_PROVIDER` 决定默认厂商
- 每次命令也可以临时覆盖 provider / model / base URL
- 非 OpenAI provider 当前默认关闭 tracing

## 稳定 Demo 样本

当前建议使用的稳定样本：

- `therapist_id=56`
- `plan_id=6`
- `patient_id=146`

## 启动与使用

推荐运行环境见：

- [envIntro.md](</d:/Project/metaAgent/envIntro.md>)

当前常用启动方式：

```powershell
D:\APP\ANACONDA\envs\metaAgent\python.exe Demo\main.py
```

服务模式示例：

```text
review-patient --plan-id 6 --days 30
screen-risk --therapist-id 56 --days 30
weekly-report --therapist-id 56 --days 30
帮我复核计划 6
看一下医生 56 最近 30 天的高风险患者
给我这个医生最近 7 天的周报
换成最近 7 天
```

运行时切换 LLM：

```text
set-provider qwen
set-model qwen-plus
show-llm
set-agent on
review-patient --plan-id 6 --days 30
clear-llm
```

单次命令示例：

```powershell
D:\APP\ANACONDA\envs\metaAgent\python.exe Demo\cli.py review-patient --plan-id 6 --days 30
D:\APP\ANACONDA\envs\metaAgent\python.exe Demo\cli.py screen-risk --therapist-id 56 --days 30
D:\APP\ANACONDA\envs\metaAgent\python.exe Demo\cli.py weekly-report --therapist-id 56 --days 30
```

## 数据访问与回退策略

- 数据库访问默认只读
- MySQL 不可用时可回退到 mock 数据
- 当前已确认真实 MySQL 链路可读
- 当前 demo 默认使用真实 A 链数据做稳定样本回归

## 当前限制

- 风险评分仍是规则版，不是学习版
- `dbdevicelog.PlanId` 仍需逻辑清洗，不能当严格 session 主键
- `dbrehaplan.StartTime` 不可直接作为精确计划开始时间
- A 链结果层仍以当前结构化抽取为主，尚未深入 `dbreportdata`
- B 链虽然已保留独立证据块，但尚未做成独立步道复核入口
- OpenAI 当前配置未完全打通，Qwen 是当前默认可用 provider

## 迁移到 LangGraph

当前结构已经为迁移做了隔离：

- `repositories/ + services/` 可以原样保留
- `tools/` 的入参出参可以继续复用
- 只需要替换 `agent/orchestrator.py` 的编排层

也就是说，当前成果主语始终是：

**面向康复训练师的计划执行偏离识别与复核支持系统**

而不是某个 agent 框架本身。
