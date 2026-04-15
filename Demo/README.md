# Rehab Execution Deviation Demo

## 定位

这是一个面向康复训练师的计划执行偏离识别与复核支持 demo。

当前主链聚焦：

- `dbrehaplan`
- `dbdevicelog`
- `dbreport`
- `dbtemplates`

步道链 `dbwalk / walkreportdetails` 目前仅作为补充解释，不并入 A 链主偏离打分。

## 目录

- `Demo/main.py`: 常驻 CMD 交互入口
- `Demo/cli.py`: 单次命令入口
- `agent/`: OpenAI Agents SDK 编排
- `tools/`: tool adapter 层
- `services/`: 业务逻辑层
- `repositories/`: 只读数据库访问层
- `models/`: Pydantic schema
- `config/`: 配置读取和多厂商 LLM 配置

## 启动

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
Copy-Item .env.example .env
```

最少需要补：

- `MYSQL_PASSWORD`

如果要启用 Agents SDK，再补对应厂商的 key：

- OpenAI: `OPENAI_API_KEY`
- Qwen: `QWEN_API_KEY`
- DeepSeek: `DEEPSEEK_API_KEY`

## 多厂商 LLM 配置

`.env.example` 已支持三套配置：

- `LLM_PROVIDER=openai|qwen|deepseek`
- `OPENAI_*`
- `QWEN_*`
- `DEEPSEEK_*`

默认 provider 由 `LLM_PROVIDER` 控制，但每次命令也可以临时覆盖。

## CMD 交互

启动服务式进程：

```powershell
.\.venv\Scripts\python Demo\main.py
```

交互命令：

```text
review-patient --plan-id 6 --days 7
screen-risk --therapist-id 1623 --days 30
weekly-report --therapist-id 1623 --days 30
```

运行时切换 provider，不用重启服务：

```text
set-provider qwen
set-model qwen-plus
show-llm
review-patient --plan-id 6 --days 7

set-provider deepseek
set-model deepseek-chat
review-patient --plan-id 6 --days 7

clear-llm
```

## 单次命令方式

使用默认 provider：

```powershell
.\.venv\Scripts\python Demo\cli.py review-patient --plan-id 6 --days 7
```

临时切换到 Qwen：

```powershell
.\.venv\Scripts\python Demo\cli.py --llm-provider qwen --use-agent-sdk review-patient --plan-id 6 --days 7
```

临时切换到 DeepSeek：

```powershell
.\.venv\Scripts\python Demo\cli.py --llm-provider deepseek --use-agent-sdk weekly-report --therapist-id 1623 --days 30
```

覆盖模型名或 base URL：

```powershell
.\.venv\Scripts\python Demo\cli.py --llm-provider qwen --llm-model qwen-max --llm-base-url https://dashscope.aliyuncs.com/compatible-mode/v1 --use-agent-sdk review-patient --plan-id 6 --days 7
```

输出 JSON：

```powershell
.\.venv\Scripts\python Demo\cli.py --json --llm-provider deepseek --use-agent-sdk review-patient --plan-id 6 --days 7
```

## 当前工具

- `get_plan_summary`
- `get_execution_logs`
- `calc_deviation_metrics`
- `get_outcome_change`
- `get_gait_explanation`
- `generate_review_card`
- `screen_risk_patients`
- `generate_weekly_risk_report`
- `reflect_on_output`

## Agent 流程

1. 先分类任务
2. 再进入对应工具集
3. 输出最终结果
4. 最后做一次受约束 reflection 检查

默认策略：

- 如果显式指定了 `--llm-provider` 或 `--use-agent-sdk`，系统会尝试启用 Agents SDK
- 如果当前 provider 缺少 key 或模型配置，自动回退到 direct 模式
- 非 OpenAI provider 默认关闭 tracing，避免额外依赖 OpenAI tracing 配置

## 当前实现边界

- 数据库访问默认只读
- 若数据库不可用，可自动回退到 mock 数据
- `dbdevicelog.PlanId` 不完全可靠，因此偏离判断采用多证据合并
- 步态链只做补充解释，不参与主风险评分

## 迁移 LangGraph 的方式

当前已经把核心逻辑拆成三层：

1. `services/` 负责真正业务逻辑
2. `tools/` 只负责把 service 包装成工具
3. `agent/` 只负责 OpenAI Agents SDK 编排

后续迁移到 LangGraph 时：

- 保留 `repositories/ + services/`
- 复用 `tools/` 的入参和出参 schema
- 重写 `agent/orchestrator.py` 为 LangGraph graph 即可
