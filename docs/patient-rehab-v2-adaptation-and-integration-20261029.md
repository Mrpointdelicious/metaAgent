# 患者康复工具 v2 适配与联调

日期：2026-10-29。本次从重新 fetch 后的 MetaAgent `origin/main` 提交 `2a08efe7bedc2e8e0999fe90c268b97b1bea180b` 新建 `codex/patient-rehab-v2`。未合入本地 dify-demo 功能分支或其未提交改动。

AI_WebApi 继续使用 `codex/patient-rehab-v2-tenant-scope`，以 `5ca2bec` 及本轮基本档案引用修复为联调版本；数据库仍为当前 project 和现有 dbbuniessuser。本次未实施新库迁移。

## 统一入口

原生 `/v1/chat` 和 Dify chat-messages 协议的外层格式沿用，患者 inputs 改为唯一格式：

```json
{
  "request_id": "request-example",
  "conversation_id": "conversation-example",
  "user": "authenticated-subject",
  "query": "查询我的康复记录，并说明所属机构",
  "response_mode": "blocking",
  "inputs": {
    "patient_identity": {"type": "project_user_id", "value": "7"},
    "identity_context_token": "business-backend-issued-proof",
    "current_business_id": 1,
    "data_scope": "all_tenants"
  }
}
```

LoginName 使用 `{"type":"login_name","value":"000007"}`，完整保留字符串。旧 patientId、projectPatientId、iretourPatientId、robotDbUserId、手机号及其别名拒绝使用，不转换成新身份。

业务登录后端签发短期凭证，subject 必须与 user 一致；MetaAgent 只转发，无须持有业务签名密钥。服务 Bearer 认证仍独立执行。凭证不得由普通用户文本、模型工具参数或对话历史生成。

API 在每次请求和 status/cancel 访问前调用 AI_WebApi 的 `resolve_patient_identity`，确认匿名 patient_ref 和 scope_fingerprint。所有患者家族仅接受 `contract_version=2.0.0`，响应中的 patient_ref 和有效范围必须与本次绑定一致。非 2xx 封套保留业务 reason code，凭证过期、身份冲突与无记录分别处理。

## 查询、事实与报告

- `TrustedScope.patient_payload` 是唯一患者上下文工厂；`RunContext.call` 为 agent 和 legacy 路径统一注入主体、身份、凭证及范围。删除各设备 ID 选择和 robot 映射缓存。
- 默认 all_tenants，current_tenant 按真实记录机构筛选。范围、机构、入口身份、后端绑定版本共同进入会话/缓存隔离键；刷新同一范围的凭证不丢会话。
- 机构元数据从同一证据对象取得，Fact、记录锚点和回答保留所属医院/卫生中心。事实校验同时核对机构和范围，防止数值正确但机构被替换。未知机构不替换成当前登录机构。
- 所有 generate_* 都先读取同选择条件的分析，再由程序绑定 expected_source_snapshot；参数不暴露给模型自由填写。报告失败仍保留已取得事实。
- 制品下载 URL 完整保留 access_grant；同一 artifact_ref 获得新 URL 时重新发布卡片。模型工具消息及长期 agent_messages 不保存该授权 URL，前端通过 artifact_ready 卡片展示图片。
- 保留的旧图路径从 LangGraph runtime context 取得 TrustedScope，不再把患者编号或凭证写入 checkpoint channel。
- 命令行客户端接受 project_user_id / login_name，凭证从 `META_AGENT_IDENTITY_CONTEXT_TOKEN` 或 `--identity-token-file` 读取，控制台隐藏凭证；文件方式支持逐轮刷新。

## 本轮测试

测试分为隔离回归和显式开启的真实联调。真实患者响应、报告、配置和临时日志位于被忽略的 `.local/`，不提交 Git。

| 检查 | 结果与边界 |
| --- | --- |
| MetaAgent pytest | 273 项隔离回归 + 11 项真实 HTTP 联调，共 284 项通过；1 项 PostgreSQL 集成因未配置独立测试 DSN 跳过 |
| Ruff | src、tests、interactive.py 检查通过 |
| AI_WebApi 非 smoke 回归 | 365 项通过，包括本轮新增的基本档案匿名引用一致性测试 |
| 真实 HTTP 联调 | 11 项通过，连接独立 AI_WebApi 实例和当前 project，只执行 SELECT |
| 两种范围 | 同一已认证入口分别查询全部机构/当前机构，核对计数缩小和逐条机构标注；按记录机构筛选 |
| 真实报告 | IReGo 同次分析→快照→Chromium PNG→短期授权下载；图片确认显示记录所属机构 |
| 拒绝场景 | 过期凭证、身份不匹配、跨范围引用、缺失报告下载凭证、过时来源快照均被拒绝 |
| MetaAgent HTTP | 真正启动 uvicorn，通过 /v1/chat 触发真实后端查询，并验证 status 与范围隔离；该自动化用例的模型决策使用固定脚本 |
| 真实模型联调 | 使用现有运行环境配置的模型，两轮分别查询 all_tenants 和 current_tenant，均返回可用的 partial 答案；合计 8 次后端工具调用、5 次模型调用，无业务失败。未将来源未确认改写成无记录 |

真实联调发现基本档案仍返回 project:ID，而其他工具返回匿名 patient_ref；已在 AI_WebApi 修复并增加回归。Windows 发布物挂载 Linux 容器时缺少 Linux Playwright 驱动，已补齐专用联调环境，报告成功生成。

该样本的 IReTour 历史来源尚未核实，接口按设计报告来源覆盖不足；未将相同数字 ID 当作患者关联。本轮真实 PNG 验证覆盖 IReGo；其余报告家族通过合成响应验证快照补链，不宣称已验证真实历史数据的全部报告样式。

真实数据库只读数量对照为：同一入口账号的全部机构计划 188 条，当前机构计划 4 条；两者分别与查询返回一致。这里只记录汇总数量，不上传患者 ID、患者响应或真实报告。

## 复现

```powershell
uv run pytest --ignore=tests/integration -ra
uv run ruff check src tests interactive.py
$env:META_AGENT_LIVE_PATIENT_V2_CONFIG = 'D:/private/live-patient-v2.json'
uv run pytest tests/integration/test_patient_v2_live.py -ra
```

联调配置包含 base_url、bearer_token、identity_signing_key、patient_id、other_patient_id、current_business_id；签发密钥仅供隔离联调签名使用，不作为 MetaAgent 生产配置。可添加 expected_plan_counts 对照只读 SQL 数量。

现有演示 MetaAgent、Dify 和 AI_WebApi 容器未切换。本轮验证通过后，实际接入方仍须把经认证的业务凭证绑定到上述 inputs，再使用本版服务。
