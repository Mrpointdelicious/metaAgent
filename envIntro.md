# 运行环境与 VSCode 配置说明

本文档基于当前项目 `D:\Project\metaAgent` 在 `2026-04-15` 的实际检查结果编写。

目标：
- 总结当前运行环境状态
- 记录真实连通性测试结果
- 说明 `.env` 的关键配置项
- 详细说明 VSCode 中如何配置解释器、终端、调试和任务

## 1. 当前推荐运行环境

当前项目统一使用以下 conda 环境：

```text
D:\APP\ANACONDA\envs\metaAgent
```

当前环境状态：

- Python: `3.11.15`
- 依赖已安装：`openai-agents`、`openai`、`pydantic`、`pydantic-settings`、`PyMySQL`、`python-dotenv`
- 项目已完成 editable install：`meta-agent-rehab-demo==0.1.0`

不再推荐使用项目里的 `.venv`。它还在磁盘上，但不是当前主环境。

## 2. 本次连通性测试结果

以下结果来自实际命令，不是静态推断。

### 2.1 基础可运行性

已通过：

- `D:\APP\ANACONDA\envs\metaAgent\python.exe --version`
- `D:\APP\ANACONDA\envs\metaAgent\python.exe Demo\cli.py --help`
- `D:\APP\ANACONDA\envs\metaAgent\python.exe -m compileall config models repositories services tools agent Demo`

结论：

- Python 环境可正常启动
- CLI 入口可正常加载
- 当前代码没有明显语法错误

### 2.2 Docker 与端口状态

已通过：

- `docker version`
- `Test-NetConnection 127.0.0.1 -Port 3306`

结论：

- Docker daemon 当前已启动
- 本机 `127.0.0.1:3306` 端口可达

### 2.3 MySQL 真实连接测试

测试方式：

- 使用 `PyMySQL` 直接读取 `.env` 中的 `MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE`
- 执行 `SELECT DATABASE(), VERSION(), 1`

结果：

- TCP 层连通
- `meta_user` 可成功登录
- 当前数据库为 `meta_universe`
- 已确认存在 `dbrehaplan` 表

补充说明：

- `D:\Project\Docker\mysql\README.md` 中记录的 app user 密钥与 `D:\Project\Docker\mysql\.env` 不一致
- 实际生效的是 Docker 目录 `.env` 中的 `MYSQL_PASSWORD`
- 项目 `.env` 已经对齐到 Docker MySQL 的真实 app user 密码

### 2.4 Demo 数据链路测试

测试命令：

```powershell
D:\APP\ANACONDA\envs\metaAgent\python.exe Demo\cli.py --json review-patient --plan-id 6 --days 7
```

结果：

- CLI 可执行
- `source_backend` 为 `mysql`
- `plan_id=6` 已能返回真实患者、计划、执行和结果数据

这说明 demo 已经从 mock fallback 切换到真实 MySQL 读取链路。

补充样本：

- 当前适合作为稳定 demo 回归的医生样本是 `therapist_id=56`
- 当前适合作为单患者复核样本的计划是 `plan_id=6`

### 2.5 LLM Provider 连通性测试

#### OpenAI

测试方式：

- 使用 `.env` 中的 `OPENAI_API_KEY / OPENAI_MODEL / OPENAI_BASE_URL`
- 发起最小 chat completion 请求

结果：

- 调用失败

实际报错：

```text
NotFoundError: Error code: 404 - Invalid URL (POST /v1/responses/chat/completions)
```

这说明：

- 当前 `OPENAI_*` 配置不是完全可用状态
- 高概率是 `OPENAI_BASE_URL` 配置不正确，或者它并不是当前 SDK 期望的兼容地址
- 所以 OpenAI provider 目前不能视为“已打通”

#### Qwen

测试方式：

- 使用 `.env` 中的 `QWEN_API_KEY / QWEN_MODEL / QWEN_BASE_URL`
- 发起最小 chat completion 请求

结果：

- 调用成功

返回内容：

```text
OK
```

这说明：

- Qwen provider 当前可用
- 如果你要先跑通 agent SDK，优先建议用 Qwen

#### DeepSeek

当前未做真实调用测试。

原因：

- `DEEPSEEK_API_KEY` 仍为空

## 3. 现阶段的准确结论

当前环境不是“全部连通”，但核心数据链路已经可用。

当前已确认可用：

- conda 环境
- Python 依赖
- CLI 和编译检查
- Docker daemon
- MySQL 端口
- MySQL 真实读链路
- Qwen provider
- `Demo/main.py` 多轮自然语言交互

当前仍未打通：

- OpenAI provider
- DeepSeek provider

## 4. `.env` 配置说明

当前项目会从根目录 `.env` 读取环境变量。

关键配置项分成四类。

### 4.1 默认 LLM 配置

```env
LLM_PROVIDER=qwen
AGENTS_TRACING_ENABLED=false
```

说明：

- `LLM_PROVIDER` 控制默认模型厂商
- 当前项目默认 provider 已切换为 `qwen`
- 当前支持：`openai`、`qwen`、`deepseek`
- `AGENTS_TRACING_ENABLED` 主要用于 OpenAI 场景

### 4.2 OpenAI 配置

```env
OPENAI_API_KEY=
OPENAI_MODEL=
OPENAI_BASE_URL=
```

说明：

- 如果使用官方默认地址，`OPENAI_BASE_URL` 通常应留空
- 如果你配置了代理或兼容服务，`OPENAI_BASE_URL` 必须和当前 SDK 兼容
- 当前环境里的 OpenAI 连通性测试失败，优先检查 `OPENAI_BASE_URL`

### 4.3 Qwen 配置

```env
QWEN_API_KEY=
QWEN_MODEL=qwen-plus
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

说明：

- 当前这组配置已通过最小请求测试
- 推荐作为当前 demo 的首选 provider

### 4.4 DeepSeek 配置

```env
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

说明：

- 只有在 `DEEPSEEK_API_KEY` 填好后，才建议做真实测试

### 4.5 MySQL 配置

```env
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=meta_universe
MYSQL_USER=meta_user
MYSQL_PASSWORD=
MYSQL_CHARSET=utf8mb4
MYSQL_CONNECT_TIMEOUT=5
USE_MOCK_WHEN_DB_UNAVAILABLE=true
```

说明：

- 当前项目已可用 `meta_user` 连到真实库
- 之前的问题来自 Docker MySQL README 与实际 `.env` 中 app user 密码不一致
- 如果你暂时只想演示流程，可保留 `USE_MOCK_WHEN_DB_UNAVAILABLE=true`

## 5. VSCode 中如何配置

这部分不是泛泛说明，而是和当前仓库里的 `.vscode` 文件一致。

### 5.1 当前已写入的 VSCode 配置文件

当前仓库中已配置：

- `.vscode/settings.json`
- `.vscode/launch.json`
- `.vscode/tasks.json`

### 5.2 `settings.json` 的作用

当前内容对应以下意图：

- `python.defaultInterpreterPath`
  固定解释器为 `D:\APP\ANACONDA\envs\metaAgent\python.exe`
- `python.envFile`
  让 VSCode 的 Python 运行和调试自动读取 `${workspaceFolder}/.env`
- `python.analysis.extraPaths`
  把工作区根目录加入分析路径，减少导入告警
- `python.terminal.activateEnvironment=false`
  当前 PowerShell 里 `conda` 不一定在 `PATH`，所以不依赖自动激活，而是直接绑定解释器路径
- `python-envs.defaultEnvManager` / `python-envs.defaultPackageManager`
  告诉 VSCode 优先按 conda 方式识别环境
- `terminal.integrated.cwd`
  让终端默认落在项目根目录

### 5.3 VSCode 中选择解释器的实际步骤

如果你第一次打开项目，建议手动确认一次解释器：

1. 打开 VSCode
2. `File -> Open Folder`
3. 选择 `D:\Project\metaAgent`
4. 按 `Ctrl + Shift + P`
5. 输入 `Python: Select Interpreter`
6. 选择：

```text
D:\APP\ANACONDA\envs\metaAgent\python.exe
```

如果列表里没有这个解释器，可以选：

```text
Enter interpreter path
```

然后手动指定：

```text
D:\APP\ANACONDA\envs\metaAgent\python.exe
```

### 5.4 为什么这里不依赖终端里的 `conda activate`

因为当前系统级 PowerShell 中，`conda` 命令没有保证总是在 `PATH` 里。

所以当前策略不是：

- 先开终端
- 再赌 VSCode 能自动 `conda activate`

而是直接：

- 在 VSCode 里固定 Python 解释器路径
- 在调试和任务里也直接使用这个解释器

这样更稳定，也更适合当前机器环境。

### 5.5 `launch.json` 是怎么配置的

当前已提供三个调试入口：

- `Demo: Main Service`
- `Demo: CLI Review`
- `Demo: CLI Weekly Report`

它们的共性是：

- `cwd` 指向 `${workspaceFolder}`
- `envFile` 指向 `${workspaceFolder}/.env`
- `console` 使用 `integratedTerminal`

这意味着：

- 从 VSCode 点“运行/调试”时，会自动读取 `.env`
- 不需要你手工在调试前再导出环境变量
- 当前工作目录会保持在项目根目录，避免相对路径跑偏

### 5.6 如何在 VSCode 里启动 demo

方法 1：直接调试常驻服务

1. 打开左侧 `Run and Debug`
2. 选择 `Demo: Main Service`
3. 点击运行
4. 在终端里看到 `rehab-demo>` 提示符后输入命令

可输入：

```text
review-patient --plan-id 6 --days 7
screen-risk --therapist-id 56 --days 30
weekly-report --therapist-id 56 --days 30
帮我复核计划 6
看一下医生 56 最近 30 天的高风险患者
给我这个医生最近 7 天的周报
换成最近 7 天
```

方法 2：直接跑单次 CLI

1. 打开左侧 `Run and Debug`
2. 选择 `Demo: CLI Review` 或 `Demo: CLI Weekly Report`
3. 点击运行

### 5.7 `tasks.json` 是怎么配置的

当前已提供三个任务：

- `Demo: CLI Help`
- `Demo: Review Patient`
- `Check: Compile`

这些任务都直接调用：

```text
D:\APP\ANACONDA\envs\metaAgent\python.exe
```

而不是依赖终端激活。

运行方式：

1. 按 `Ctrl + Shift + P`
2. 输入 `Tasks: Run Task`
3. 选择你要执行的任务

### 5.8 VSCode 里 `.env` 是如何生效的

当前是两层生效：

1. `settings.json` 里的：

```json
"python.envFile": "${workspaceFolder}/.env"
```

2. `launch.json` 里的：

```json
"envFile": "${workspaceFolder}/.env"
```

这样做的目的：

- 平时 Python 扩展读取 `.env`
- 调试启动时也明确读取 `.env`

即使其中一层没有生效，另一层也能兜底。

### 5.9 建议安装的 VSCode 扩展

建议至少安装：

- `ms-python.python`
- `ms-python.vscode-pylance`

如果你经常编辑 `.env` 和 JSON，也可以补：

- `mikestead.dotenv`

## 6. 推荐的日常使用方式

### 6.1 命令行方式

```powershell
D:\APP\ANACONDA\envs\metaAgent\python.exe Demo\main.py
```

### 6.2 VSCode 调试方式

优先使用：

- `Demo: Main Service`

如果只想快速验证单条命令：

- `Demo: CLI Review`
- `Demo: CLI Weekly Report`

### 6.3 运行时切换 provider

在 `Demo/main.py` 启动后的交互界面中可用：

```text
set-provider qwen
set-model qwen-plus
show-llm
review-patient --plan-id 6 --days 7
```

## 7. 当前需要你继续处理的项

如果你要把环境进一步打通到“真实数据 + 多模型都可用”的状态，还需要处理两件事：

1. 修正 `OPENAI_BASE_URL`
   当前错误是 `Invalid URL (POST /v1/responses/chat/completions)`
2. 补齐 `DEEPSEEK_API_KEY`

当前最值得优先修的是第 1 项，因为 Qwen 已经可用，而 OpenAI 仍然是配置错误状态。
