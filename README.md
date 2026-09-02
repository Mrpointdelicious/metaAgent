# MetaAgent

面向 IREGO、IREMO 等康复数据源的独立智能体编排服务。项目采用标准 Python `src` 布局，核心使用 LangGraph，外层提供 Dify Chatflow/Workflow 兼容接口。

## 当前能力

- Dify 风格的阻塞与 SSE 流式请求。
- `[answer]`、`[mode]`、`[image]` 旧前端标签适配。
- IREGO 查询、训练分析和按需报表的统合 Workflow。
- 完整工具结果保存在上下文之外，图状态只保留事实、引用和患者可读文案。
- 多租户、多患者、多会话线程隔离。
- 开发环境内存持久化，生产环境 PostgreSQL Checkpoint 与 Store。
- 启动配置门、健康检查、结构化日志与基础契约测试。

## 本地开发

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev,postgres]"
Copy-Item .env.example .env
python main.py
```

接口：

- `GET /health/live`
- `GET /health/ready`
- `GET /health/dependencies`
- `POST /compat/dify/v1/chat-messages`
- `POST /compat/dify/v1/workflows/run`

## 容器运行

生产运行前需要把 `.env.example` 复制为 `.env`，填写服务 Token、AI_WebApi Token 与 PostgreSQL 密码，并设置：

```dotenv
META_AGENT__APP_ENV=production
META_AGENT__DRY_RUN=false
META_AGENT__PERSISTENCE_BACKEND=postgres
```

然后运行：

```powershell
docker compose config
docker compose up -d --build
```

如果 `AI_WebApi` 和 MetaAgent 位于同一 Docker 网络，应将 `META_AGENT__AI_WEBAPI_BASE_URL` 改为容器服务名地址；当前默认的 `host.docker.internal:5043` 用于通过宿主机端口访问。
