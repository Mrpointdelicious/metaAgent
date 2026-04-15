# MetaAgent Rehab Review Demo

面向康复训练师的计划执行偏离识别与复核支持系统 demo。

当前目标不是通用医疗聊天，也不是纯 RAG，而是围绕院内康复训练流程，帮助治疗师快速回答这些问题：

- 哪些患者最近出现了计划执行偏离
- 偏离主要体现在哪些维度
- 偏离是否伴随结果变化
- 哪些患者值得优先人工复核

## 当前能力

支持三类任务：

1. 单患者复核
2. 多患者风险筛选
3. 周报 / 风险摘要

当前主分析链路以 A 链为主：

- `dbrehaplan`
- `dbdevicelog`
- `dbreport`
- `dbtemplates`

步道链当前只作为补充解释：

- `dbwalk`
- `walkreport`
- `walkreportdetails`

它不会直接进入 A 链主风险评分。

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
  计划层摘要与计划任务解析
- `execution_service.py`
  执行日志汇总
- `outcome_service.py`
  结果变化与报告解析
- `gait_service.py`
  步态补充解释
- `deviation_service.py`
  偏离指标计算
- `report_service.py`
  复核卡片与周报聚合
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
  任务分类、tool 调用、provider 切换、direct fallback

### 5. `Demo/`

命令行入口。

- `Demo/main.py`
  常驻 CMD 交互服务入口
- `Demo/cli.py`
  单次命令入口
- `Demo/README.md`
  Demo 层使用说明

### 6. `models/` 与 `config/`

- `models/`
  Pydantic 数据结构
- `config/settings.py`
  环境变量读取与多厂商 LLM 配置解析

## Agent 流程

当前 agent 工作流是受控的：

1. `plan`
   判断请求属于单患者复核、多患者筛选还是周报
2. `execute`
   调用对应工具链
3. `review`
   做受约束 reflection 检查
4. `final`
   输出结构化结果和可读摘要

如果没有可用 LLM key，或者当前 provider 配置不完整，系统会自动回退到 direct 模式，直接调用 service 层生成结果。

## 偏离指标

当前先做规则化版本，包含四类一级指标：

- 到训率
- 完成率
- 执行剂量偏差
- 连续中断风险

这四项都尽量映射到现有数据库字段，不做脱离数据边界的空想功能。

## 多厂商 LLM 支持

当前已支持至少三种 provider：

- `openai`
- `qwen`
- `deepseek`

配置入口在 `.env`：

```env
LLM_PROVIDER=openai
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

## 安装与启动

### 1. 创建虚拟环境

```powershell
python -m venv .venv
```

### 2. 安装依赖

```powershell
.\.venv\Scripts\python -m pip install -e .
```

### 3. 准备配置

```powershell
Copy-Item .env.example .env
```

至少补这些配置：

- `MYSQL_PASSWORD`
- 如果要启用 Agents SDK，再补对应 provider 的 API key

### 4. 启动交互服务

```powershell
.\.venv\Scripts\python Demo\main.py
```

## 使用方法

### 方法 1：服务模式

启动后输入：

```text
review-patient --plan-id 6 --days 7
screen-risk --therapist-id 1623 --days 30
weekly-report --therapist-id 1623 --days 30
```

运行时切换 LLM：

```text
set-provider qwen
set-model qwen-plus
show-llm
review-patient --plan-id 6 --days 7

set-provider deepseek
set-model deepseek-chat
weekly-report --therapist-id 1623 --days 30

clear-llm
```

### 方法 2：单次命令

单患者复核：

```powershell
.\.venv\Scripts\python Demo\cli.py review-patient --plan-id 6 --days 7
```

多患者筛选：

```powershell
.\.venv\Scripts\python Demo\cli.py screen-risk --therapist-id 1623 --days 30
```

周报：

```powershell
.\.venv\Scripts\python Demo\cli.py weekly-report --therapist-id 1623 --days 30
```

使用 Qwen：

```powershell
.\.venv\Scripts\python Demo\cli.py --llm-provider qwen --use-agent-sdk review-patient --plan-id 6 --days 7
```

使用 DeepSeek：

```powershell
.\.venv\Scripts\python Demo\cli.py --llm-provider deepseek --use-agent-sdk weekly-report --therapist-id 1623 --days 30
```

覆盖模型或 base URL：

```powershell
.\.venv\Scripts\python Demo\cli.py --llm-provider qwen --llm-model qwen-max --llm-base-url https://dashscope.aliyuncs.com/compatible-mode/v1 --use-agent-sdk review-patient --plan-id 6 --days 7
```

JSON 输出：

```powershell
.\.venv\Scripts\python Demo\cli.py --json review-patient --plan-id 6 --days 7
```

## 输出说明

普通文本输出会展示：

- `execution_mode`
- `llm_provider`
- `llm_model`

JSON 输出会额外返回：

- `task_type`
- `structured_output`
- `final_text`

## 数据访问与回退策略

- 数据库访问默认只读
- 如果数据库当前不可用，自动回退到 mock 数据
- 因为 `dbdevicelog.PlanId` 不完全可靠，执行证据采用多证据合并策略
- 步态链只做补充解释，不直接参与主偏离评分

## 当前限制

- 风险评分仍是规则版，不是学习版
- 结果层仍以现有报告结构化字段为主，尚未深入 `dbreportdata`
- 步态链没有并入 A 链主模型
- 当数据库服务未启动时，只能演示 mock fallback
- 真实 Agents SDK 调用需要当前 provider 配置有效 key

## 迁移到 LangGraph

当前结构已经为迁移做了隔离：

- `repositories/ + services/` 可以原样保留
- `tools/` 的入参出参可以继续复用
- 只需要替换 `agent/orchestrator.py` 的编排层

也就是说，当前成果主语仍然是“康复训练师侧计划执行偏离识别与复核支持系统”，不是某个 agent 框架本身。
