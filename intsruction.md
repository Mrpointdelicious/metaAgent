# 运行时组件说明与功能流程

本文档只描述当前项目在**运行时**各组件分别承担什么功能、各环节如何调用下游模块，以及三类任务在系统中的真实执行路径。

本文档对应的系统主语是：

**面向康复训练师的计划执行偏离识别与复核支持系统**

不是通用聊天 agent，也不是把 OpenAI Agents SDK 当作成果本身。

## 1. 运行时总目标

系统在运行时主要完成三类任务：

1. 单患者复核
2. 多患者风险筛选
3. 周报 / 风险摘要

当前主任务只针对 **A 链下肢康复机器人产品链** 做偏离识别与风险判断。

当前 **B 链康复步道产品链** 在运行时只作为独立证据块保留，用于输出步态/步道专项解释，不参与当前 A 链风险分计算。

## 2. 运行模式

系统运行时有两种执行模式：

### 2.1 direct 模式

特点：

- 不依赖 LLM
- 直接调用 `services/` 生成结构化结果和最终文本
- 是当前最稳定的主执行路径

### 2.2 agents_sdk 模式

特点：

- 由 `OpenAI Agents SDK` 做受控工具编排
- 工具层仍然调用同一套 `services/`
- 当前 `structured_output` 仍会先由 direct 链路生成
- Agent 主要负责：工具调用、编排、以及最终文本生成
- 如果 Agent 调用失败，自动回退为 `direct_fallback`

这个实现细节非常重要：

**即使开启 Agents SDK，当前结构化结果也不是先由 agent“推理出来”，而是先由 service 链稳定计算出来。**

## 3. 运行时组件总览

### 3.1 `config/`

核心文件：

- `config/settings.py`

运行时职责：

- 读取 `.env`
- 解析数据库连接配置
- 解析默认 LLM provider
- 解析 runtime provider override
- 提供稳定 demo 样本配置

运行时输出：

- `Settings`
- `ResolvedLLMConfig`

运行时被谁调用：

- `Demo/main.py`
- `Demo/cli.py`
- `agent/orchestrator.py`
- `repositories/db_client.py`
- 各 service 初始化阶段

### 3.2 `Demo/`

核心文件：

- `Demo/main.py`
- `Demo/cli.py`
- `Demo/dialogue.py`

运行时职责：

- 接收用户输入
- 把输入转成 `OrchestratorRequest`
- 调用 `RehabAgentOrchestrator`
- 打印结果

三者分工：

- `Demo/cli.py`
  负责单次命令解析和输出渲染
- `Demo/main.py`
  负责常驻交互循环、上下文维护、运行时切换 provider、自然语言入口
- `Demo/dialogue.py`
  负责自然语言意图识别、会话状态续接、启动命令容错、跟进问题解析

### 3.3 `agent/`

核心文件：

- `agent/schemas.py`
- `agent/instructions.py`
- `agent/orchestrator.py`

运行时职责：

- 定义统一请求与响应结构
- 初始化 repository / services / tools
- 判断任务类型
- 判断走 direct 还是 agents_sdk
- 汇总最终输出

当前运行时总入口：

- `RehabAgentOrchestrator.run`

### 3.4 `tools/`

核心文件：

- `plan_tools.py`
- `execution_tools.py`
- `outcome_tools.py`
- `gait_tools.py`
- `report_tools.py`
- `reflection_tools.py`

运行时职责：

- 把 `services/` 包装成 agent 可调用工具
- 不承载核心业务逻辑
- 只在 `agents_sdk` 模式下被 Agent 显式调用

### 3.5 `services/`

核心文件：

- `plan_service.py`
- `execution_service.py`
- `outcome_service.py`
- `gait_service.py`
- `deviation_service.py`
- `report_service.py`
- `reflection_service.py`
- `shared.py`

运行时职责：

- 承载所有核心业务逻辑
- 负责时间窗、数据解析、指标计算、风险判断、复核卡生成、周报聚合

### 3.6 `repositories/`

核心文件：

- `db_client.py`
- `rehab_repository.py`
- `mock_data.py`

运行时职责：

- 只读 MySQL 访问
- 统一 SQL 查询
- 数据库不可用时回退 mock

### 3.7 `models/`

运行时职责：

- 作为 service 与 orchestrator 之间的结构化数据契约
- 保证单患者复核卡、风险患者摘要、周报结构、reflection 输出稳定

## 4. 系统启动时会做什么

### 4.1 CLI 单次命令启动

入口：

- `Demo/cli.py`

启动顺序：

1. 解析命令行参数
2. 调用 `build_request_from_args`
3. 初始化 `RehabAgentOrchestrator`
4. 调用 `orchestrator.run`
5. 输出 `structured_output` 或 `final_text`

### 4.2 常驻交互模式启动

入口：

- `Demo/main.py`

启动顺序：

1. 设置控制台编码为 UTF-8
2. 读取 `Settings`
3. 初始化命令解析器
4. 初始化 `RehabAgentOrchestrator`
5. 初始化 runtime LLM 覆盖状态
6. 初始化 `ConversationState`
7. 进入 `rehab-demo>` 循环

交互循环中会做的事：

- 识别是否为系统命令
- 识别是否为结构化命令
- 识别是否为自然语言
- 把结果回写到上下文状态

## 5. Orchestrator 在运行时做什么

核心类：

- `RehabAgentOrchestrator`

初始化时做的事：

1. 保存 `Settings`
2. 初始化 `RehabRepository`
3. 初始化所有 service
4. 初始化单患者工具集
5. 初始化群组任务工具集

### 5.1 初始化出的组件依赖图

`RehabAgentOrchestrator`
-> `RehabRepository`
-> `PlanService`
-> `ExecutionService`
-> `OutcomeService`
-> `GaitService`
-> `DeviationService`
-> `ReflectionService`
-> `ReportService`

然后再构造：

- `single_review_tools`
- `group_tools`

### 5.2 `run()` 的运行时职责

`run()` 进入后会按下面顺序执行：

1. `classify_request`
2. `settings.resolve_llm_config`
3. 判断任务是否支持
4. 先执行 `_run_direct` 生成 `structured_output`
5. 判断是否允许启用 `agents_sdk`
6. 如果不启用，直接 `_render_output`
7. 如果启用，构造 Agent 并尝试运行
8. 如果 Agent 失败，回退到 direct 文本输出

### 5.3 当前最关键的实现特征

当前 `run()` 的行为不是：

- “先让 agent 推理，再查工具”

而是：

- “先用 service 链算出稳定结构化结果，再决定是否用 agent 生成最终文本”

这意味着：

- service 层是主业务执行引擎
- agent 层是可替换的编排壳

## 6. Repository 在运行时做什么

核心类：

- `MySQLReadOnlyClient`
- `RehabRepository`

### 6.1 `MySQLReadOnlyClient`

运行时职责：

- 只读连接 MySQL
- 执行 SQL
- 在 session 中设置 `SET SESSION TRANSACTION READ ONLY`

如果数据库连接失败：

- 抛出 `DatabaseConnectionError`

### 6.2 `RehabRepository`

运行时职责：

- 统一封装所有 SQL 查询
- 查询 A 链计划、执行、报告
- 查询 B 链步道执行与报告明细
- 记录最后一次使用的 backend：`mysql` 或 `mock`

运行时回退机制：

1. 先尝试真实 MySQL
2. 若失败且 `use_mock_when_db_unavailable=true`
3. 自动从 `mock_data.py` 取 mock 结果

### 6.3 当前 repository 提供的主要运行时能力

- `get_plan_anchor`
- `get_walk_anchor`
- `get_plan_records`
- `get_execution_logs`
- `get_reports`
- `get_walk_sessions`
- `get_walk_report_details`

## 7. Service 在运行时分别做什么

### 7.1 `PlanService`

输入：

- `patient_id`
- `plan_id`
- `therapist_id`
- `days / start / end`

运行时职责：

- 决定时间窗
- 查询 `dbrehaplan + dbtemplates`
- 解析 `Details`
- 解析模板任务
- 汇总计划会话

输出：

- `PlanSummary`

它是后续多个 service 的上游输入。

### 7.2 `ExecutionService`

输入：

- `patient_id / therapist_id / plan_id`
- `PlanSummary`

运行时职责：

- 查询 `dbdevicelog`
- 汇总执行日志
- 汇总按计划的执行分钟数
- 汇总任务类型分布

输出：

- `ExecutionSummary`

### 7.3 `OutcomeService`

输入：

- `patient_id / therapist_id / plan_id`
- `PlanSummary`

运行时职责：

- 查询 `dbreport`
- 解析 `ReportDetails`
- 区分训练型结果与其他结构
- 生成训练时长、步行距离、游戏分等趋势摘要

输出：

- `OutcomeChangeSummary`

### 7.4 `GaitService`

输入：

- `patient_id`
- `days / start / end`
- 可选 `item_id`

运行时职责：

- 查询 `dbwalk`
- 查询 `walkreportdetails`
- 从 B 链抽取完成率、正确率、距离、平均速度等解释指标

输出：

- `GaitExplanationSummary`

当前边界：

- 这是 B 链独立证据块
- 不参与 A 链风险计算

### 7.5 `DeviationService`

输入：

- `PlanSummary`
- `ExecutionSummary`
- `OutcomeChangeSummary`

运行时职责：

- 计算到训率
- 计算完成率
- 计算剂量达成率和剂量偏差
- 计算连续中断风险
- 计算总风险分与风险等级
- 生成 driver flags

输出：

- `DeviationMetrics`

当前实现边界：

- 指标仅按 A 链定义
- 不引入 B 链字段

### 7.6 `ReflectionService`

输入：

- `ReviewCard`

运行时职责：

- 检查证据是否充分
- 检查关键字段是否缺失
- 检查风险标签与证据是否一致
- 判断是否建议人工确认

输出：

- `ReflectionResult`

当前 reflection 是受约束检查，不是自由反思系统。

### 7.7 `ReportService`

这是当前运行时的核心业务聚合器。

主要职责：

- 生成单患者复核卡
- 生成多患者风险列表
- 生成周报

它本身不做底层 SQL，而是向下编排：

- `PlanService`
- `ExecutionService`
- `OutcomeService`
- `GaitService`
- `DeviationService`
- `ReflectionService`

## 8. Tool Adapter 在运行时做什么

tool adapter 只在 `agents_sdk` 模式下显式参与。

### 8.1 `plan_tools.py`

暴露：

- `get_plan_summary`

作用：

- 让 agent 能单独取 A 链计划层摘要

### 8.2 `execution_tools.py`

暴露：

- `get_execution_logs`
- `calc_deviation_metrics`

作用：

- 让 agent 能拿执行层证据
- 让 agent 能直接拿偏离指标结果

### 8.3 `outcome_tools.py`

暴露：

- `get_outcome_change`

作用：

- 让 agent 能拿 A 链结果变化摘要

### 8.4 `gait_tools.py`

暴露：

- `get_gait_explanation`

作用：

- 让 agent 能拿 B 链独立解释证据

### 8.5 `report_tools.py`

暴露：

- `generate_review_card`
- `screen_risk_patients`
- `generate_weekly_risk_report`

作用：

- 让 agent 直接调用顶层业务能力

### 8.6 `reflection_tools.py`

暴露：

- `reflect_on_output`

作用：

- 让 agent 可以显式调用当前单患者输出的约束检查

## 9. 三类任务的真实运行流程

### 9.1 单患者复核

用户输入来源：

- CLI：`review-patient --plan-id 6 --days 30`
- 对话：`帮我复核计划 6`

真实调用顺序：

1. `Demo/main.py` 或 `Demo/cli.py`
2. 组装 `OrchestratorRequest(task_type="single_review")`
3. `RehabAgentOrchestrator.run`
4. `ReportService.generate_review_card`
5. `PlanService.get_plan_summary`
6. `ExecutionService.get_execution_logs`
7. `OutcomeService.get_outcome_change`
8. `GaitService.get_gait_explanation`
9. `DeviationService.calc_deviation_metrics`
10. `ReflectionService.reflect_on_output`
11. 返回 `ReviewCard`
12. `orchestrator._render_review_card`
13. 输出文本

如果启用 `agents_sdk`：

- 第 4 到第 10 步仍然会先在 `_run_direct` 中执行一次
- 然后 Agent 再根据工具调用生成最终文本

### 9.2 多患者风险筛选

用户输入来源：

- CLI：`screen-risk --therapist-id 56 --days 30`
- 对话：`看一下医生 56 最近 30 天的高风险患者`

真实调用顺序：

1. `Demo/main.py` 或 `Demo/cli.py`
2. 组装 `OrchestratorRequest(task_type="risk_screen")`
3. `RehabAgentOrchestrator.run`
4. `ReportService.screen_risk_patients`
5. `ReportService.generate_weekly_risk_report`
6. `build_time_range`
7. `RehabRepository.get_plan_records`
8. 提取患者列表
9. 对每个患者调用 `generate_review_card`
10. 汇总为 `PatientRiskSummary[]`
11. `_render_risk_screen`
12. 输出文本

这个流程说明：

- 多患者筛选本质上是“批量单患者复核 + 排序”

### 9.3 周报 / 风险摘要

用户输入来源：

- CLI：`weekly-report --therapist-id 56 --days 30`
- 对话：`给我这个医生最近 7 天的周报`

真实调用顺序：

1. `Demo/main.py` 或 `Demo/cli.py`
2. 组装 `OrchestratorRequest(task_type="weekly_report")`
3. `RehabAgentOrchestrator.run`
4. `ReportService.generate_weekly_risk_report`
5. `build_time_range`
6. `RehabRepository.get_plan_records`
7. 对患者逐个调用 `generate_review_card`
8. 聚合统计
9. 形成 `WeeklyRiskReport`
10. `_render_weekly_report`
11. 输出文本

这个流程说明：

- 周报不是独立模型
- 它是患者级复核结果的聚合视图

## 10. 多轮对话在运行时怎么工作

多轮对话只发生在：

- `Demo/main.py`

### 10.1 上下文状态

状态由：

- `Demo/dialogue.py -> ConversationState`

保存内容：

- `task_type`
- `patient_id`
- `plan_id`
- `therapist_id`
- `days`
- `top_k`

### 10.2 自然语言解析

由：

- `parse_natural_language_request`

负责：

- 从文本中识别任务类型
- 提取 `doctor / therapist / patient / plan / days / top_k`
- 识别 follow-up 请求
- 在必要时回填稳定 demo 样本

### 10.3 多轮续接

多轮续接由：

- `update_state_from_response`

负责：

- 从当前请求更新上下文
- 从实际响应里反写 `patient_id / plan_id / therapist_id`

所以像下面这种对话能工作：

1. `看一下医生 56 最近 30 天的高风险患者`
2. `换成最近 7 天`
3. `给我这个医生的周报`

第二句和第三句都不是重新从零解析，而是依赖已保存的上下文状态。

## 11. A 链 / B 链在运行时的真实边界

当前运行时边界必须明确：

- A 链负责当前主任务
- B 链只作为独立专项证据存在

### 11.1 A 链在运行时被哪些模块使用

- `PlanService`
- `ExecutionService`
- `OutcomeService`
- `DeviationService`
- `ReportService`

### 11.2 B 链在运行时被哪些模块使用

- `GaitService`

### 11.3 B 链当前不会进入哪些环节

- 不进入 `DeviationService.calc_deviation_metrics`
- 不进入 `risk_score` 计算
- 不进入 `weekly_report` 聚合统计
- 不进入当前一级偏离指标定义

### 11.4 为什么代码里仍然会看到 B 链字段

因为当前单患者复核卡需要预留独立解释位。

当前保留方式是：

- `ReviewCard.gait_explanation`

这不是 A/B 融合，而是：

- “当前主任务仍是 A 链，但系统保留了 B 链专项证据位”

## 12. 当前稳定运行的关键点

当前运行时最关键的设计点有四个：

1. 核心业务逻辑在 `services/`，不绑死在 agent 框架中
2. `ReportService.generate_review_card` 是最核心的复用单元
3. `structured_output` 先由 direct service 链生成，保证稳定性
4. A/B 链在业务定义和运行时实现上都保持独立边界

## 13. 一句话总结

当前系统在运行时的真实工作方式是：

**输入层把命令或自然语言转成统一请求，编排层决定任务类型与执行模式，service 层完成真正的 A 链偏离识别与复核生成，B 链只以独立证据块形式输出，最后再由 direct 或 agent 文本层生成最终结果。**
