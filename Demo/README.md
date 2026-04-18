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
