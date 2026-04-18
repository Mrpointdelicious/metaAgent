# Demo Entries

`Demo/` 现在只保留演示与本地调试入口，不再承担正式主链路由、身份判定或权限来源。

所有正式请求都应通过：

```text
server.request_factory
-> RehabAgentOrchestrator
-> IntentRouter / LLMRouter
-> strategy chooser
-> fixed_workflow / template_analytics / agent_planned
```

## 医生演示入口

```bash
python Demo/doctor_demo.py --doctor-id 56
```

或单次问题：

```bash
python Demo/doctor_demo.py --doctor-id 56 --question "看一下最近7天高风险患者"
```

### 真实数据库推荐医生 ID

以下 ID 来自当前 MySQL 真实库统计，按 `dbrehaplan` 计划数、`dbdevicelog` 设备日志数、`dbreport` 报告数、患者覆盖数综合排序。`DoctorId=0` 和明显测试账号未作为 Demo 示例。

| doctor_id | dbuser.Name | 计划数 | 患者数 | 设备日志数 | 报告数 | 最近计划时间 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `56` | `wanghaiyun` | 856 | 120 | 1262 | 577 | 2025-02-28 |
| `83` | `王小明` | 515 | 82 | 703 | 369 | 2025-02-27 |
| `285` | `陈小朵` | 42 | 7 | 55 | 18 | 2024-10-16 |

演示时可优先使用：

```bash
python Demo/doctor_demo.py --doctor-id 56 --question "看一下最近7天高风险患者"
python Demo/doctor_demo.py --doctor-id 83 --question "给我最近30天的周报"
python Demo/doctor_demo.py --doctor-id 285 --question "查询医生285的名字"
```

特点：

- 必须显式传入 `--doctor-id`。
- Demo 构造医生身份 `SessionIdentityContext`。
- Demo 不推断正式 task type。
- Demo 不使用默认治疗师兜底。
- 最终请求统一走 `server.request_factory` 和核心 orchestrator。

## 患者演示入口

```bash
python Demo/patient_demo.py --patient-id 20001
```

或单次问题：

```bash
python Demo/patient_demo.py --patient-id 20001 --question "患者20001叫什么"
```

特点：

- 必须显式传入 `--patient-id`。
- Demo 构造患者身份 `SessionIdentityContext`。
- 患者身份不能进入多患者风险筛选或医生聚合。
- 最终请求统一走正式 orchestrator 主链。

## 身份感知用户查询示例

Demo 只负责显式注入身份，用户查询的权限判断由 service/repository 层完成。当前新增 3 个窄工具：

- `lookup_accessible_user_name(user_id)`：查询当前身份可访问用户的姓名。
- `list_my_patients(days=None)`：医生会话列出与自己相关的患者。
- `list_my_doctors(days=None)`：患者会话列出与自己相关的医生。

医生会话下 Agent 白名单只包含 `lookup_accessible_user_name` 和 `list_my_patients`；患者会话下只包含 `lookup_accessible_user_name` 和 `list_my_doctors`。不允许通过 prompt 或 Agent 自行判断权限，也不暴露直接查询 `dbuser` 的工具。

示例：

```bash
python Demo/doctor_demo.py --doctor-id 56 --question "我的名字"
python Demo/doctor_demo.py --doctor-id 56 --question "列出我所有的患者"
python Demo/patient_demo.py --patient-id 20001 --question "列出和我有关的医生"
```

## Legacy Debug Shell

```bash
python Demo/main.py
```

`Demo/main.py` 是 legacy/local debug shell，仅用于历史兼容和手工实验。它不是生产入口，也不应被当作正式问题路由器。

`Demo/dialogue.py` 仅作为历史兼容代码保留；正式入口和当前 legacy debug shell 都不依赖它做路由、身份判定或权限控制。

## 正式服务入口

生产/接口场景应使用：

```bash
python server/main.py
```

从 stdin 传入 JSON payload，例如：

```json
{
  "doctor_id": 30001,
  "question": "查询医生30001的名字"
}
```

## 路由说明

正式问题路由只在核心层完成：

- `agent/intent_router.py`：规则主判。
- `agent/llm_router.py`：低置信或模糊场景 refine。

Demo 层只负责读取本地参数、构造身份化 request、调用 orchestrator、打印输出。

## Session 模拟前端行为

`Demo/doctor_demo.py` 和 `Demo/patient_demo.py` 现在按前端调用方式工作：启动时确定一组身份、`session_id` 和 `conversation_id`，之后整个交互循环都复用同一组字段。Demo 不拼接聊天历史，也不维护正式记忆逻辑；原始历史由 `server/session_manager.py` 提供的 SDK session 管理。

医生端：

```bash
python Demo/doctor_demo.py --doctor-id 56 --session-id s1 --conversation-id c1
```

患者端：

```bash
python Demo/patient_demo.py --patient-id 20001 --session-id patient-s1 --conversation-id patient-c1
```

如果不传 `--session-id` 或 `--conversation-id`，Demo 会在启动时通过 `server.request_factory.ensure_session_ids(...)` 生成一次，并在当前进程的所有轮次中保持不变。每一轮都会重新构造一个前端 payload，再交给 `build_orchestrator_request_from_payload(...)` 和正式 orchestrator 主链。

`Demo/main.py` 仍是 legacy/local debug shell，但也遵循同样原则：显式身份、固定 session/conversation、无手工历史拼接。

## 结果集 Follow-up 演示

连续追问依赖两层服务端能力：

- SDK session 保存原始历史。
- result-set artifact / active result set 保存上一轮可复用集合。

Demo 只模拟前端持续携带同一组身份和会话字段，不自己拼接历史，也不自己维护工作集。比如医生端启动后连续输入：

```bash
python Demo/doctor_demo.py --doctor-id 56 --session-id s1 --conversation-id c1
```

```text
查询我所有的患者
这些患者中哪些在这30天内有训练？
显示他们完成计划的具体时间
```

第一轮名单类工具会在服务端注册 `active_result_set`；后续“这些患者 / 他们”会由 `IntentRouter` 命中 `result_set_query`，再调用 `filter_result_set_*` 或 `enrich_result_set_*` 工具。若当前线程没有 active result set，服务端返回 `followup.missing_active_result_set`，不会由 Demo 兜底改写问题。
