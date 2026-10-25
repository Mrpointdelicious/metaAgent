# Agent 输出上限的 env 配置

`META_AGENT__AGENT_MAX_OUTPUT_TOKENS` 独立控制单 Agent 每次模型调用的最大输出 token 数。
开发与本地部署显式设置为 4096；可填写 128～8192 的整数，非法值在配置加载时拒绝。

```dotenv
META_AGENT__AGENT_MAX_OUTPUT_TOKENS=4096
META_AGENT__PLANNER_MAX_TOKENS=1024
```

新变量未设置时回退到 `PLANNER_MAX_TOKENS`，后者未设置时使用原代码默认值 2048。
不要将新变量写成空字符串；需要兼容回退时删除或注释整行。
Planner 和 legacy 答案生成沿用原配置，不受新 Agent 变量覆盖。

Agent 模型创建与上下文保护共用 `effective_agent_max_output_tokens`，避免实际可输出 4096，
上下文预算却只预留 1024。此值是每次调用的输出上限，不是整段对话或输入上下文总预算。
较高上限不会强制生成到该长度；模型正常结束时仍提前停止。

## 生效文件与更新

本地开发使用仓库根目录 `.env`。当前 Docker Compose 部署使用
`outputs/20261020-docker-cutover/metaagent.env`，由部署覆盖文件的 `env_file` 指定。
两份文件包含本地凭据，不提交版本库；`.env.example` 提供新配置示例。

首次上线这项功能需要新镜像。之后只改 env 时，沿当前部署的 Compose 参数重新创建
`meta_agent` 服务即可，不需要再次构建镜像。仅执行 `docker restart` 不会重新读取
Compose 的 env_file。Dify 工作流和 RAG 配置不需要调整。

## 截断检测

模型最终消息的 `finish_reason=length` 表示回答没有完整生成。系统保留已生成文本，并发出
“本次回答因输出长度限制未能完整生成”的提示；原本成功的运行与 goal 改为 `partial`，
已有 unavailable 等失败状态不会被提升为成功或部分成功。不自动重试或续写。

运行 metrics 记录：

- `agent_max_output_tokens`：本轮有效输出上限。
- `agent_model_steps[].finish_reason`：各次模型调用的结束原因。
- `agent_answer_finish_reason`、`agent_answer_truncated`：最终回答的结束原因与截断标记。

这些信息经现有原生响应提供。Dify 沿用现有状态及进度事件展示，不新增协议字段。

## 验证

```powershell
uv run pytest tests/test_agent_output_budget.py tests/test_single_agent.py -q
```

测试覆盖新变量优先、旧配置回退、边界值、模型实际参数、上下文预算预留、正常结束与截断
时回答/进度/goal/completed 的一致性。端到端使用已有场景、患者与 RAG 多轮问题集，
核对模型结束原因与耗时，不将 HTTP 200 或工作流 succeeded 单独作为通过依据。
