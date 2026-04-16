# Demo 使用说明

`Demo/` 提供 MetaAgent 的两个运行入口：

- `Demo/main.py`：常驻交互式入口，适合自然语言、多轮续接和现场演示
- `Demo/cli.py`：单次命令入口，适合脚本、回归和快速验证

当前 Demo 使用的真实主链是：

```text
输入
-> OrchestratorRequest
-> IntentRouter 规则路由
-> LLMRouter 按需精修
-> choose_execution_strategy
-> fixed_workflow / template_analytics / agent_planned
-> OrchestratorResponse
```

`direct` 与 `agents_sdk` 只是运行模式。策略选择由 `ExecutionStrategy` 统一决定。

## 快速启动

在项目根目录执行：

```powershell
python -m pip install -e .
python Demo\main.py
```

也可以直接执行单次命令：

```powershell
python Demo\cli.py review-patient --plan-id 6 --days 30
python Demo\cli.py screen-risk --therapist-id 56 --days 30
python Demo\cli.py weekly-report --therapist-id 56 --days 30
```

## 交互模式

启动：

```powershell
python Demo\main.py
```

常用输入：

```text
review-patient --plan-id 6 --days 30
screen-risk --therapist-id 56 --days 30
weekly-report --therapist-id 56 --days 30
帮我复核计划 6
看一下医生 56 最近 30 天的高风险患者
给我这个医生最近 7 天的周报
换成最近 7 天
```

运行时配置：

```text
set-provider qwen
set-model qwen-plus
set-base-url https://dashscope.aliyuncs.com/compatible-mode/v1
set-agent on
set-agent off
set-trace on
show-llm
clear-llm
show-context
clear-context
show-demo-sample
```

## 单次命令

固定任务：

```powershell
python Demo\cli.py review-patient --plan-id 6 --days 30
python Demo\cli.py screen-risk --therapist-id 56 --days 30 --top-k 10
python Demo\cli.py weekly-report --therapist-id 56 --days 7
```

开放分析：

```powershell
python Demo\cli.py ask "查看医生56这30天有哪些以前来过的患者没有来"
python Demo\cli.py ask "看医生56这30天有哪些是前80-30天以前来过的患者，这30没有来"
python Demo\cli.py ask "查询一下这30天哪些医生有定患者训练计划？"
```

启用 SDK 和 trace：

```powershell
python Demo\cli.py --use-agent-sdk --show-trace ask "看医生56这30天有哪些是前80-30天以前来过的患者，这30没有来"
python Demo\cli.py --json --show-trace ask "查询一下这30天哪些医生有定患者训练计划？"
```

临时切换 LLM：

```powershell
python Demo\cli.py --llm-provider qwen --llm-model qwen-plus --use-agent-sdk ask "查询一下这30天哪些医生有定患者训练计划？"
python Demo\cli.py --llm-provider deepseek --llm-model deepseek-chat --use-agent-sdk ask "看医生56这30天有哪些是前80-30天以前来过的患者，这30没有来"
```

## 输出说明

所有命令最终返回 `OrchestratorResponse`。

人类可读输出主要看：

- `success`
- `execution_mode`
- `final_text`

JSON / trace 模式下还会看到：

- `structured_output`
- `validation_issues`
- `execution_trace`

开放分析会在 `structured_output.planned_query_source` 标记计划来源：

- `fixed_template`
- `agents_sdk_runtime`
- `llm_planner`
- `fallback_template`

## Demo 样本

当前建议使用的稳定样本：

- `therapist_id=56`
- `plan_id=6`
- `patient_id=146`

## 当前边界

- 固定 workflow 是高频主路径，不会被 planner 替代
- LLM Router 只精修路由，不执行工具
- `agent_planned` 会优先进入真实 Agents SDK runtime，只使用开放分析 primitive tool 白名单
- LLM Planner 只生成结构化计划，不访问数据库、不生成 SQL
- Plan Validator 会拦截非法工具、非法 scope、SQL-like 文本和参数错误
- 数据库不可用时可按配置回退到 mock 数据
- B 链步道数据目前只作为 `gait_explanation` 独立证据块，不参与 A 链风险评分
