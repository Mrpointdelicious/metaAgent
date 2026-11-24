# MetaAgent

当前 Agent/main 整合版本的入口、会话隔离和部署条件见
[整合接入说明](docs/agent-main-integration.md)。下文历史部署记录不表示本次已切换线上服务。

前台区分患者、医生、学生，后台保留运营人员。学生传 `inputs.role=student` 或 `inputs.identity=学生`；
四份独立角色策略共享运行时。运营仅能从独立后台凭证进入；临床操作和后台指标查询还需要由业务方核验当前就诊/机构。接入字段、知识发布、严格模式及回退见[四角色 Agent 方案](docs/role-agent-plan.md)。

## 单 Agent 与三层患者工具（2026-10）

默认 `ORCHESTRATION_MODE=agent` 使用 LangChain `create_agent` 创建的 LangGraph。
一个模型在“回答或调用工具”的循环中自主选择查询、查看失败结果和调整参数重试，
最终直接生成自然语言回答。每轮只自动装填一份可缓存的基本档案，其余查询由模型决定。
中间件管理可信身份、可用工具、历史上下文、证据时效、工具结果裁剪和调用预算，
不再要求模型先生成固定计划，也不使用事实模板拼接患者回答。

三层工具已经在配套 AI_WebApi 中实现并向 Agent 暴露：

- `get_patient_profile`：`project.dbuser.UserDetails` 中的基本信息与档案自述病史，白名单排除姓名和电话。
- `get_patient_consultation`：`dbmedical` 病历、诊断、医嘱、关联处方及 `dbconsultationinfo` 就诊记录，支持分页。
- `get_patient_rehab`：`dbrehaplan` 康复计划，以及 IReTour/IReGo 训练历史。保留各设备的可用状态，区分计划与实际结果。

某一设备查询失败时，其他层仍可读取已确认身份下的可用数据。工具失败作为结果返回模型，
模型可调整查询或回答已有信息；真实失败仍保留在运行记录中。
`AGENT_MAX_MODEL_CALLS=8` 限制模型调用，`MAX_TOOL_CALLS=16` 限制后端调用，
最后一次模型调用只要求形成回答。本地 Knowledge 查询另受模型调用预算和总执行时限约束。
`ORCHESTRATION_MODE=legacy` 可回退到原有 Planner/Compiler/Scheduler 和领域工作流。

已接入 IReTour 的概览、分页历史、单次分析和连续趋势，
以及医院运营查询/报表和统一患者身份解析。IReGo、医生、场景和知识工具保留。
IReTour 历史与 IReGo 一样通过可信患者身份、分页查询、Evidence 和多轮工具上下文接入，
使用独立的 `record_scope=all/with_result/without_result`、项目、训练状态和结果状态筛选。
`IRETOUR_REPORTS_ENABLED=false` 默认关闭 IReTour 单次及趋势报表：模型工具列表不暴露，
Agent、legacy 和底层客户端均禁止调用。报表实现保留，IReGo 报表不受影响。
`MULTISOURCE_PATIENT_CONTEXT_ENABLED=false` 默认禁用多源患者上下文，
同时阻止每轮链首装填、显式患者概况和底层端点调用。旧实现保留，可由配置重新启用。

患者工具统一使用 `2.0.0`，所有设备共用 `patient_identity`、业务签发的短期
`identity_context_token` 和 `data_scope`。登录名保留前导零；旧 patientId、projectPatientId、
iretourPatientId 和手机号别名均被拒绝。请求先经 AI_WebApi 确认匿名 patient_ref 和
scope_fingerprint，再进入模型运行；运行中凭证仅用于工具调用，不进入模型或历史。
患者康复默认 all_tenants，可选 current_tenant。事实、记录锚点和报告保留实际记录的
医院/卫生中心，账号或查询范围改变时隔离历史。业务凭证刷新不改变同一授权范围的会话。
医院运营指标默认限于有机构授权的后台 `operator`。患者 v2 请求、签发边界和验证见
[患者康复 v2 适配及联调](docs/patient-rehab-v2-adaptation-and-integration-20261029.md)。

趋势分析支持 2–20 次，生成图片支持 3–20 次，可用连续序号或最多180天的日期范围。
“这次/上一次/下一页”保留设备来源；报表失败保留已经获取的事实。
IReTour 单位未知时原样标为未知，数值变化不转换为临床疗效结论。

已有真实测试在独立的本地 AI_WebApi 实例上执行，数据库对照通过只读查询取得。
单 Agent 记录包括10个单轮冒烟用例、12轮连续对话和12次三层接口直连查询。
病史与建议问答只保存原始文本，未评价临床诊断或康复建议的医学正确性。
本地联调脚本、含真实患者信息的 Excel、数据记录和报表原图不进入 Git；
`outputs/` 已由 `.gitignore` 排除。Runtime Cutover 的历史验收记录见
[Runtime Cutover Merge Readiness](docs/runtime-cutover-merge-readiness.md)，最新 Docker 部署验证见本文“验证”。

配套 AI_WebApi 使用当前 project 数据库，身份与机构关系读取现有 dbbuniessuser；
不迁移用户、计划或报告数据。工具基地址保持 `/api/ai/patients/tools`。
本次联调使用独立的本地 25043 后端实例，现有 Dify 与演示服务未切换到此版本。

## Dify 展示入口

当前本机 Dify 已接入统一的“康复与就诊助手”，通过原生 `POST /v1/chat` 调用 MetaAgent，
使用 `response_mode=blocking`。Chatflow 为：开始 → 准备可信请求 → HTTP 调用 → 整理回答 → 展示。
患者绑定、租户、角色和空间信息由服务端配置注入；服务 Bearer Token 保存在 Dify secret 环境变量中。
工具选择、模型调用和连续对话由 MetaAgent 的单 Agent 运行时完成。

Dify 的 `METAAGENT_URL` 配置为 `http://meta_agent:8080`，通过共享 Docker 网络访问服务。
页面先展示最终回答，中间步骤统一附在回答末尾，不展示原始工具 JSON；
`show_workflow_steps=false`、`prompt_public=false`。迁移保留了原应用、展示链接和患者绑定。
Dify RAG 尚未接入。服务对外采用 Dify chat-messages 契约：`POST /v1/chat-messages`（以及别名
`/v1/chat`、`/compat/dify/v1/chat-messages`）接收 Dify 请求、返回 `message`/`message_end` 事件；
`/compat/dify/v1/workflows/run` 仍返回 HTTP 501。

`workflows/irego.py` 和 `graph/builder.py` 的保留路径同样使用 v2；运行凭证通过
LangGraph runtime context 注入，不进入可序列化状态。默认容器使用单 Agent 入口。执行目标和动作是数组，没有五个固定输出槽位。

## legacy 模式的 IReGo 工作流

Planner 只决定业务目标（`IReGoRequest`：operation/selector/topics/need_artifact/force_refresh），
不规划 IReGo 内部工具步骤：LLM 无权生成 `after_goal_ids`/`condition` 依赖边，Compiler 不再把模型声明的依赖提升为执行约束。
Scheduler 只看到一个 `irego.execute` 任务，内部固定链由 `domains/irego.py` 决定：

- `overview`：患者概览 → Evidence → Fact/FactView（带缓存）
- `history`：训练历史 → Evidence → Fact/FactView
- `session`：定位 session_ref（复用新鲜锚点/最新可用/历史发现）→ 会话分析 → Evidence → Fact/FactView → 更新记录锚点 → `need_artifact=true` 时重读同一 session_ref 的分析并绑定新快照生成报告
- `trend`：连续窗口趋势 → Evidence → Fact/FactView → `need_artifact=true` 时生成趋势报告

只要进入 IReGo 就必然执行对应 operation 的基础数据查询并建立事实层（"LLM 不需要读"不等于"不获取"）；
报告只是可选制品，失败不会抹掉已建立的事实（目标降级为 partial）。
回答由 Context Selector 从事实层选取，模型只选择事实编号。报告只由 `need_artifact` 控制，绝不因用户只索取解释而生成。

## 宿主机开发运行（可选）

当前本机演示服务使用 Docker，宿主机旧服务已停用。以下命令供单独开发调试使用：

```powershell
uv sync --locked --extra dev --extra postgres --extra llm-deepseek
# 仅首次创建配置；已有 .env 时保留原文件。
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
uv run python main.py
```

默认 `DRY_RUN=true` 使用明确标记的合成工具数据，不返回伪造图片或真实场景动作。
单 Agent 仍需真实模型，配置 `DEEPSEEK_API_KEY`，`AGENT_MODEL` 留空时使用 `PLANNER_MODEL`。
接真实 AI_WebApi 时设置 `DRY_RUN=false` 和后端地址/令牌，并部署配套三层工具。
`PLANNER_MODE`、`ANSWER_MODE` 只影响 legacy 模式；`deterministic` 使用确定性目标解析，
`template`/`llm_select` 使用原来的事实回答模块。

`main.py` 使用兼容 Windows PostgreSQL 驱动的 Selector 循环。直接使用 Uvicorn CLI 且在 Windows 接 PostgreSQL 时，添加 `--loop meta_agent.infrastructure.event_loop:loop_factory`。Linux 容器使用默认事件循环。

## 接口（Dify chat-messages 兼容）

可信网关携带服务 Bearer Token（`META_AGENT__SERVICE_BEARER_TOKEN`，即 Dify 调用方的 `api_key`），
注入真实用户、租户、角色、患者及当前空间。身份不从用户问题中提取；服务令牌不能直接分发给患者客户端。

### 多接入方令牌与功能流程

除 `primary` / `secondary` 两个固定令牌外，可用 `META_AGENT__SERVICE_CLIENTS` 配置任意数量的
功能令牌（单行 JSON 数组）。每个条目对应一个独立客户端档案。网关在 `inputs` 中同时传入已核验的 `role` 或 `identity`。医生的专用临床流程需要 `consultation_id` 和该次就诊的 `resource_context_token`。各流程所需能力见[四角色 Agent 方案](docs/role-agent-plan.md)。

| 字段 | 作用 |
| --- | --- |
| `name` | 客户端标识，进入 `scope_hash` 与会话隔离键；不同令牌的会话互不可见 |
| `token` | Bearer 令牌，全局唯一，不能与旧 primary/secondary 重复 |
| `flow` | 绑定的独立流程分支：`default`、`consultation_room`，或 `ai_summary` / `ai_medical_order` / `ai_prescription` / `ai_realtime_analysis` / `ai_consultation` 五个独立功能流程；留空则沿用 `pos` 是否含「问诊房间」判定 |
| `allowed_tools` | 工具白名单，非空时只有列出的工具可见 |
| `excluded_tools` | 工具黑名单，与 `allowed_tools` 互斥 |
| `prompt_suffix` | 追加在系统提示词末尾的功能边界，只能收窄能力，不能覆盖安全约束 |

功能令牌绑定的是**独立注册的完整流程分支**（不是问诊内的子流程）：每个 `ai_*` 分支在
`flows/` 下有自己的 Flow 与完整执行链路（登录、取数、生成、下发），会话类型、goal、
metrics 都按独立分支记账：

- `ai_summary` 纪要总结：时间归一 → 语义过滤与拼接 → LLM 两级总结 → 拆 part/all（见下）；
- `ai_medical_order` 医嘱生成：记录数组 → 医患判定 → LLM 抽取 → 引号规范化，产出三字段 JSON（见下）；
- `ai_prescription` 处方推荐（新建处方）：二次判断 → 结构化抽取 → 合并参数 → 查处方 / 补 Type → 排班推荐（见下）；
- `ai_realtime_analysis` 实时分析（对齐 Dify「初始评估.yml」双模式）：query 含「初始评估」
  或 JSON 报文 `InitialEvaluation` 字段非空 →
  登录 → 病历信息 / 用户角色 / 病历模板 / 历史就诊记录四路取数 → LLM 生成结构化初始评估，
  并缓存进会话变量 `patient_context`；否则 → 实时对话分析。前端真实 query 是 JSON 字符串
  （`{"InitialEvaluation":null,"role":"Doctor","timeStamp":…,"from":"医生1","message":"…"}`），
  对话模式会解析报文、取 `message`（带说话人前缀）作为对话原文；纯文本 query 原样使用。
  与缓存的初始评估基线对比，50-200 字建议。两种模式输出均为
  `{"event":"analysis_record","display_text":"[Answer:]…","payload":{}}` 事件 JSON；
- `ai_consultation` 问诊全流程：任务分类 → 按类别路由子流程（与问诊分支同一实现）。

配置错误（重名、重令牌、同时配置黑白名单、未注册 flow）会在启动时拒绝，不会静默降级。

`allowed_tools` 与 `prompt_suffix` 只作用于走 Agent 的路径（`default` 分支、问诊内的
基础问答/指令）；独立功能流程（`ai_*` 分支）使用各自的专用提示词和取数链路，
不读通用系统提示词。

示例（单行 JSON，实际部署按 app 替换令牌）：

```text
META_AGENT__SERVICE_CLIENTS='[{"name":"summary","token":"app-...","flow":"ai_summary","allowed_tools":["open_chat_history","search_knowledge"]},{"name":"medical-order","token":"app-...","flow":"ai_medical_order"},{"name":"prescription","token":"app-...","flow":"ai_prescription"},{"name":"realtime-analysis","token":"app-...","flow":"ai_realtime_analysis"},{"name":"consultation","token":"app-...","flow":"ai_consultation"}]'
```

纪要总结与评估分析都只产出草稿文本，不写病历、医嘱、处方或量表，也不声称已保存或生效。
评估分析沿用问诊的登录与取数接口，患者定位字段来自业务授权方核验的本次就诊。

`ai_summary` 与 Dify「AI纪要总结」工作流逐步对齐，链路固定为四段：

1. **取数**：优先看 `query` 本身是不是对话记录数组（见下）；其次看 `inputs` 的
   `records` / `dialogue` / `data` / `messages` / `chat_records`，随后使用授权方返回的
   本次就诊记录或摘要。缺少记录时使用当前会话轮次。
2. **时间归一**（`textproc.normalize_records`）：把 `timeStamp` 秒/毫秒时间戳、以及正文里
   `起止时间：HH:MM:SS-HH:MM:SS`（按 UTC 理解）统一换成北京时间。
3. **对话整合**（`textproc.integrate`）：判断本轮输入有无医疗语义，与历史累积拼接，
   逐句剥掉「嗯嗯/哈哈/谢谢」等敷衍尾巴，并给出 `exceed_six`（超过 6 句即需分段）。
   结构化记录（`role` / `from` / `message`）先渲染成
   `[2026-11-16 12:44:14] 潘x（医生）：…` 再送模型，模型据此判断发言人与时间区间。
4. **LLM + 后处理**：用「部分对话总结 / 全部对话总结」提示词生成纯文本，
   `parse.parse_summary` 拆成两段；若记录自带 `timeStamp`，`parse.apply_time_range`
   会把每条小结的「起止时间」校正为记录的真实区间（模型常写成 `00:00:00`）。
   `answer` 就是 `{"part": "...", "all": "..."}` 的 JSON 字符串（对应 Dify 侧 `summary` 变量的契约）。

「AI纪要总结」的输入示例（`inputs` 为空，只有 `query`）：

```json
["{\"role\":\"Doctor\",\"timeStamp\":1794804254821,\"from\":\"潘x\",\"message\":\"我膝盖疼了两周，走路多了更疼。。\"}",
 "{\"role\":\"Doctor\",\"timeStamp\":1794804267261,\"from\":\"潘x\",\"message\":\"先做个膝关节功能评估，暂时别急着用药。。\"}"]
```

输出（时间区间取自记录的 `timeStamp`，换算成北京时间）：

```json
{"part": "[潘x]：患者自述膝关节疼痛持续两周，行走增多时疼痛加重……（起止时间：12:44:14-12:44:27）\n",
 "all": "患者膝关节疼痛持续两周，行走增多时疼痛加重，医生建议先行膝关节功能评估，暂缓用药……\n"}
```

`ai_medical_order`（AI医嘱生成）同样是「记录数组进、JSON 文本出」，四段链路：

1. **代码 A**（`ai_medical_order.transcript.conversation_text`）：记录数组 → 对话文本，
   每行 `医生(医生1)：…` / `患者(医生1)：…`（`role` 定医生/患者，`from` 是名字，两者不纠正）；
2. **条件判断**：文本里既没有「医生」也没有「患者」→ 不调模型，固定输出
   `{"ChiefComplaint": "无问诊记录", "Diagnosis": "无问诊记录", "Management": "无问诊记录"}`；
3. **LLM**（`medical_order_prompt.py`，原文对齐）：只按文本抽取，产出
   `{"ChiefComplaint": …, "Diagnosis": …, "Management": …}`；
4. **代码 B**（`transcript.to_json_text` / `build_order_json`）：去 Markdown 围栏 →
   中文/单引号统一成英文双引号 → 结构位中文标点（`，：；`）修正 → 按固定键序规整，
   最终 `answer` 就是这三个字段的**纯 JSON 对象**（不再套 `{"data": …}` 外壳）。

```json
// 输入 query（inputs 为空）
["{\"role\":\"Patient\",\"timeStamp\":1793085016130,\"from\":\"医生1\",\"message\":\"有吗？\"}",
 "{\"role\":\"Patient\",\"timeStamp\":1793085283680,\"from\":\"医生1\",\"message\":\"脑卒中\"}"]

// 输出 answer
{"ChiefComplaint": "脑卒中", "Diagnosis": "脑卒中", "Management": "立即就医评估；监测生命体征；保持呼吸道通畅；避免自行用药；遵医嘱进行影像学检查及相应治疗。"}
```

键序固定为 `ChiefComplaint` → `Diagnosis` → `Management`，模型漏字段补空串、多余字段保留在后面；
模型没吐合法 JSON 时如实返回原文（`metrics.medical_order_json = "raw"`），不编造字段。

这条分支不再反查 webyun（对话内容由调用方随 `query` 传入），因此也不需要
`patientphone` / `requesterphone`。

`ai_prescription`（AI推荐处方 / 新建处方）对齐「元宇宙实时问诊AI(新建处方)」，共六段：

1. **二次判断**（LLM）：本轮输入有没有医学信息 / 地点 / 时间，都没有直接回空结果；
2. **整合对话**：会话变量 `talk` + `" 。"` + 本轮 `query`（与 Dify 同名变量语义一致）；
3. **结构化抽取**（LLM）：疾病（仅康复相关，感冒发烧一律 null）、机器人类型、训练模式、
   疾病判断依据、地点、时间、量表（12 个固定量表）；
4. **合并参数**：新值优先、旧 `params_json` 兜底 → 训练模式按 31 条游戏库随机挑游戏 →
   设备映射（单臂 `iReMo`，行走/坐站/平衡 `iReGo`）→ `is_multi_disease`（0/1/≥2）；
5. **查处方**：`GET GetPrescriptionList?keyword=疾病`，缺 Type（0 线上通用 / 1 线下 /
   2 评估 / 3 治疗 / 4 用药）时仅在 `prescription_auto_create` 已启用且有 `clinical.prescription.create` 授权时调用 `POST AddPrescription`，
   建完重查；知识检索（`knowledge_corpus_path` 的已批准语料）只用于补评估/治疗/用药内容；
6. **输出**：逐条 `GetPrescriptionInfoById` 取详情 → 线上按量表聚合、线下算空闲时段 →
   `answer` 为 `{"online_prescriptions": [...], "offline_prescriptions": [...], "summary": "..."}`。

```json
// 输入 query（inputs 里的 token 可选，缺省用服务账号登录）
"患者脑卒中后走路不稳，想在上海做康复训练，明天下午有号吗？"

// 输出 answer（截断示意）
{"online_prescriptions": [{"Id": 12, "Name": "脑卒中线上通用处方", "Type": 0, "Details": ["ADL量表-Barthel指数"]}],
 "offline_prescriptions": [{"Id": 13, "Name": "脑卒中线下机器人处方", "Type": 1,
   "Hospitals": [{"Name": "上海金矢机器人", "occupied_slots": ["2026/11/17 09:00-10:00"]}],
   "time_slot_recommendation": [{"Hospital": "上海金矢机器人", "Time": "2026/11/17 14:00-15:00"}]}],
 "summary": "根据走路不稳推荐相关处方。线上处方共1个：[…]。线下处方共1个：[…]，推荐地点：… 推荐时间：…。"}
```

两个开关（`.env`）：

| 配置 | 默认 | 作用 |
| --- | --- | --- |
| `META_AGENT__PRESCRIPTION_AUTO_CREATE` | `false` | 缺 Type 时是否自动 `AddPrescription`。默认只读，先跑一轮看推荐结果再打开 |
| `META_AGENT__PRESCRIPTION_API_BASE` | `http://192.168.8.71:8085` | 处方接口基址（Dify 侧用的是公网网关 `www.ga-robot.com`） |

写入副作用需要明确知道：打开自动新建后，同一疾病每次「缺 Type」都会再建一批处方
（Dify 原逻辑没有幂等去重，唯一保护是「Type 齐了就不再建」）；`SettingTime` 随机
20–30 分钟、游戏随机抽取，所以同样的输入两次结果可能不同。`dry_run=true` 时不发任何
处方请求，返回内置演示处方（`metrics.prescription_source = "demo"`），便于离线看输出结构。

### 本地令牌测试台（不启动正式服务）

不想起 Docker / 正式服务时，可用本地测试台在网页上逐个验证功能令牌：

```bash
# 离线桩：不访问外部系统、不产生模型费用（dry_run=true，临床接口返回 demo 数据）
.venv/Scripts/python.exe scripts/token_lab.py

# 真实输入输出：真实模型 + dry_run=false 的真实临床接口（会消耗额度、访问院内服务）
.venv/Scripts/python.exe scripts/token_lab.py --live \
  --patientphone 13800000000 --requesterphone 13800000000 --spaceid 1
```

`--live` 与离线桩的差别只有两点：不安装模型/管线桩，且把 `dry_run` 置为 `false`
（离线模式下 `dry_run=true` 会让 `ai_webapi` 直接返回 demo 数据，模型再真也是假输入）。

**各令牌的默认 `inputs` 是分开的**，放在 `scripts/lab_client_inputs.json`（已在
`.gitignore`，因为里面有网关 `apikey` 等凭据），结构为 `{"<令牌名>": {...}}`：

```json
{
  "primary":   {"spaceid": "67ecd511-…", "pos": "二楼 问诊中心", "requesterphone": "xwjsd0", "…": "…"},
  "secondary": {"spaceid": "d23735f8-…", "pos": "社交岛",       "requesterphone": "xwjsd0", "…": "…"},
  "summary":   {}
}
```

- 出现在文件里的令牌**以文件为准**（`summary` 给空对象即「只发 `query`」）；
- 没出现的令牌才用命令行 `--inputs` / `--patientphone` 等兜底；
- `--client-inputs <路径>` 换一份配置，`--no-client-inputs` 忽略该文件。

`--patientphone` / `--requesterphone` / `--spaceid` / `--serialnumber` 只是把真实测试数据
预填进未单独配置的卡片的 `inputs`，页面上仍可逐卡修改。临床分支不加这些参数也能跑，
但取不到病历与对话记录，模型只能在“资料缺失”的前提下作答。

网关真实报文可以直接整包喂进来（JSON 字符串或 `@文件`），只作用于没有分令牌配置的卡片：

```bash
.venv/Scripts/python.exe scripts/token_lab.py --live \
  --inputs @C:/Users/me/lab_inputs.json
```

页面顶部还有「统一 inputs」折叠框：粘贴网关报文后点「应用到全部卡片」即可一次改完所有卡
（会覆盖各令牌的专属默认值，可点「清除本地修改，恢复各令牌默认」还原）。注意报文里的
`apikey` / `apitoken` / `companykb` / `maxkapp` / `maxkbauth` 服务端**不会使用**（场景与知识库凭据走
`META_AGENT__SCENE_BACKEND_*` / `KNOWLEDGE_SOURCES` 配置），只有 `spaceid`、`identity`、
`pos`、`timestamp`、`requesterphone`（及 `patientphone`）会真正参与路由与取数。

测试台在进程内构造真实应用：认证、分支选择、会话绑定、SSE 编码都走完整代码；
持久化强制 `memory`，不写数据库。页面支持逐卡运行、一键全部运行、观察
`conversation_id` 续聊，并可切换 blocking / streaming。临床侧默认只调用只读接口
（登录、病历、对话记录、处方列表；医嘱生成与纪要总结的输入由 `query` 自带，不再取数），
医嘱 / 分析 / 纪要只产出草稿文本；处方分支只有显式打开
`META_AGENT__PRESCRIPTION_AUTO_CREATE` 才会写 `AddPrescription`，默认只读。

页面上的令牌列表直接来自 `api.security.client_registry`，因此**旧版
`META_AGENT__SERVICE_BEARER_TOKEN` / `..._SECONDARY` 与 `SERVICE_CLIENTS` 里的功能令牌会一起列出**
（旧令牌标注「旧令牌」，并显示 `secondary_excluded_tools` 的禁用项）。全量共 2 + N 个令牌，
与线上认证完全同源，不存在「页面有但服务端不认」的偏差。

聊天请求 `POST /v1/chat-messages`（别名 `/v1/chat`、`/compat/dify/v1/chat-messages`）：

```json
{
  "inputs": {
    "tenantId": "hospital-a",
    "role": "patient",
    "patient_identity": {"type": "project_user_id", "value": "3799"},
    "identity_context_token": "<业务后端签发的短期凭证>"
  },
  "query": "解读最近训练并生成报告图片",
  "response_mode": "streaming",
  "conversation_id": "conversation-001",
  "user": "trusted-actor",
  "files": [],
  "auto_generate_name": true
}
```

- `response_mode=blocking` 返回 `{"event":"message","task_id","message_id","conversation_id","mode":"chat","answer","metadata","created_at"}`；`answer` 为最终答复（consultation_room 各子流程为 JSON 事件字符串）。
- `response_mode=streaming` 返回 SSE：`data: {"event":"message",...}` → `data: {"event":"message_end",...}`，中间 `event: ping` 保活；失败发 `data: {"event":"error",...}`。
- `conversation_id` 可省略，首次请求在响应中返回生成值，续聊必须复用；相同 `conversation_id`（同一患者作用域）的会话内容聚合存储。

历史查询：

- `GET /v1/conversations?user=...&last_id=&limit=20`：列出该用户的会话（按 `updated_at` 降序，`last_id` 游标分页）。
- `GET /v1/messages?user=...&conversation_id=...`：返回该会话的消息（`query`/`answer` 逐条）。

管理与健康：

- `POST /v1/runs/{run_id}/status`、`POST /v1/runs/{run_id}/cancel`：请求体为相同的 `user`、`inputs`；不同作用域返回 404。
- `GET /health/live`、`/health/ready`、`/health/dependencies`：分别检查进程、持久化与实例锁、外部服务。

场景动作需显式启用 `SCENE_ACTIONS_ENABLED=true`，并提供 `spaceId`、`sceneVersion`；医生查询只返回数量和姓名，不自动联系医生。名单口径分两条：**房间外挂号**走 `GET /api/users`（在线接诊医生，**严格按 `spaceId` 过滤**，空间传错必然「暂无在线医生」），**房间内邀请医生**走 `GET /api/users/all`（在房间中的医生，**可见范围由 `inviterPhone` 决定**），两者不可混用：房间外只发 `spaceId`，`inviterPhone` 传了也不影响结果（`/api/users` 不认它），因此不要带。例外是**明确挂号**：当本轮出现挂号/挂某位医生的号/就他/选第 N 个等表述、在线医生恰好 1 位、该医生有手机号且请求人不是医生身份时，`[DoctorRecommend:]手机号` 作为独立 `action_ready` 事件下发（流式模式下先于 `[Answer:]` 的单独 message），由前端弹挂号卡；只问名单、多医生、无手机号、医生身份一律不下发。

## 持久化与部署

生产必须配置服务/后端令牌，关闭 `DRY_RUN`，使用 PostgreSQL。v1 只支持单 worker 和单副本，同一数据库的第二实例会因 advisory lock 拒绝启动。实例锁连接失效后停止新操作。部署时显式执行迁移，运行时不做生产 DDL。

### 当前本机 Docker 部署（2026-10-08）

Compose 项目名为 `metaagent-runtime`，Docker Desktop 在该项目下统一显示以下独立容器：

| Compose 服务 | 容器名 | 职责 | 宿主机端口 |
| --- | --- | --- | --- |
| `meta_agent` | `meta_agent` | 单 Agent、工具编排、对话和上下文 | `5080 → 8080` |
| `ai_webapi_runtime` | `meta_agent_webapi` | 新版 AI_WebApi、患者和训练查询、报表生成 | `15043 → 8080` |
| `postgres` | `metaagent-runtime-postgres-1` | 运行记录、Evidence、会话及 Checkpoint 持久化 | 不发布；Docker 内部 `5432` |

`metaagent-runtime` 是 Compose 项目分组；“单 Agent”描述模型编排方式，配套业务接口和数据库各自运行。
PostgreSQL 的容器名按“项目名、服务名、实例序号”生成。三个服务可统一管理，也可分别查看日志、更新和重启；
启动时等待依赖健康，容器通过服务名通信。

MetaAgent 通过 `http://ai_webapi_runtime:8080` 访问新版后端，通过 `postgres:5432` 访问独立数据库。
该 PostgreSQL 与 Dify 的数据库分开管理，不占用 Dify 数据库。
数据库使用命名卷 `metaagent-runtime_meta_agent_postgres_data`；患者报表和医院报表也分别使用命名卷，
重建应用容器时保留数据。删除数据卷会删除其中的数据，日常停启保留这些卷。

当前部署在仓库 `compose.yaml` 上叠加本机私有配置：

- `outputs/20261008-docker-cutover/compose.override.yaml`：新版 AI_WebApi、报表卷及外部网络。
- 同目录的 `compose.env`、`metaagent.env`、`backend.env`：Compose、MetaAgent 和后端的本机配置。
- `start-docker.ps1`：启动依赖、显式运行数据库迁移，再启动 MetaAgent。

这些配置和测试资产位于已忽略的 `outputs/` 下，不进入 Git；新检出仓库需另行准备配置。
在当前本机、仓库根目录执行：

```powershell
& ./outputs/20261008-docker-cutover/start-docker.ps1

# 后续管理必须使用相同项目名和配置文件。
$runtimeComposeArgs = @(
    'compose', '--project-name', 'metaagent-runtime',
    '--env-file', 'outputs/20261008-docker-cutover/compose.env',
    '-f', 'compose.yaml',
    '-f', 'outputs/20261008-docker-cutover/compose.override.yaml'
)
docker @runtimeComposeArgs ps
docker @runtimeComposeArgs logs --tail 100 meta_agent
docker @runtimeComposeArgs logs --tail 100 postgres
```

`GET http://localhost:5080/health/ready` 检查持久化和实例锁，
`GET http://localhost:5080/health/dependencies` 检查 AI_WebApi。
当前 MetaAgent、AI_WebApi 和 PostgreSQL 均配置为 `restart: unless-stopped`；
旧演示启动脚本已改为调用 Docker 启动脚本，宿主机不再启动对应 Python/.NET 服务。

MetaAgent 与 Dify API 使用共享网络 `docker_default`，新版 AI_WebApi 与既有 MySQL 使用 `mysql_default`。
这两个外部网络需已存在；移植部署时应按目标环境调整。
报表 `PublicBaseUrl` 需同时能被 MetaAgent 容器和展示浏览器访问，并加入 `ARTIFACT_ALLOWED_HOSTS`。
宿主机地址变更时需同步本机后端配置及白名单。

### 新环境的基础 Compose 部署

仓库内的 `compose.yaml` 提供 MetaAgent、PostgreSQL 和一次性 `migrate` 服务；新版 AI_WebApi 及 Dify 网络由部署覆盖文件补充。
`.env.example` 的默认值用于开发，不会自动启用生产 PostgreSQL 持久化。先准备 `.env`，至少设置：

- `META_AGENT__APP_ENV=production`、`META_AGENT__DRY_RUN=false`。
- `META_AGENT__PERSISTENCE_BACKEND=postgres`、`META_AGENT__AUTO_SETUP_PERSISTENCE=false`。
- `META_AGENT_POSTGRES_PASSWORD`，以及使用相同密码、主机名 `postgres`、数据库名 `meta_agent` 的 `META_AGENT__POSTGRES_DSN`。
- 服务与 AI_WebApi Bearer Token、模型 API Key、后端地址和制品主机白名单。
- `META_AGENT__WORKER_COUNT=1`、`META_AGENT__PLANNER_MAX_RETRIES=0`。

完成配置后，在新环境显式选择项目名并执行：

```powershell
docker compose --project-name metaagent-runtime build meta_agent
docker compose --project-name metaagent-runtime up -d --wait postgres
docker compose --project-name metaagent-runtime --profile maintenance run --rm migrate
docker compose --project-name metaagent-runtime up -d --wait meta_agent
```

基础 Compose 命令不包含当前本机的覆盖配置；管理当前已运行的服务使用上一节的命令。
迁移也可直接运行 `uv run python -m meta_agent.infrastructure.migrate`，需使用与目标实例一致且可访问数据库的配置。

### 持久化行为

Agent Checkpoint 保存该次运行的模型消息和裁剪后的工具消息；完整工具 JSON 存在按作用域隔离的证据库。
会话按患者和可信身份隔离，保留最近六轮，另受 `AGENT_HISTORY_TOKENS=8000` 限制，闲置24小时失效。
工具消息关联的证据过期时会标记为需要重新查询，基本档案缓存5分钟。
证据和运行默认30分钟，匿名运行审计7天。到期读拒绝并删除，每小时物理清理，运行删除同时删除 Checkpoint。
重启后重复请求返回保守的中断快照，不自动重发动作或制品。
运行时依赖、令牌和可信身份对象不进入 Checkpoint。

## 知识语料

当前 `search_knowledge` 使用 `KnowledgeAdapter → 本地 approved JSON corpus → BM25`，
仍是简化实现。Dify RAG、独立 RAG Service、Hybrid Retrieval 和 Reranker 均未接入。

`KNOWLEDGE_CORPUS_PATH` 指向本地 JSON。格式由
[domains/knowledge.py](src/meta_agent/domains/knowledge.py) 中的
`Corpus`、`Document`、`Section` 定义：顶层 `schema_version="1.0"` 和 `documents`，
文档包含领域、版本、来源、审批信息及分节文本。
只有 `approved=true` 且填写 `approved_by` 的对应领域资料参与检索。
使用中文二元片段和英文词的 BM25，并在无结果时执行一次确定性同义替换补检索，
返回原文和 `doc_id@version#section_id`。语料不可用或没有匹配时返回 unavailable。
文献文件夹中的研究论文不会自动成为已批准的患者知识库。

## 已知限制

- 当前只支持单 worker、单副本；跨实例租约与事件回放不在 v1 范围内。
- Knowledge backend 仍为本地 approved JSON corpus + BM25，尚不是生产级知识服务。
- 自动测试不验收临床诊断或康复建议的医学正确性。
- 受历史上下文预算及证据时效影响，部分结构化训练结果可能在后续轮重新查询。
- Dify 展示已通过原生 API 接入，兼容入口未启用；正式 Dify RAG 留给独立 `feature/knowledge-dify`。

详见 [Runtime Cutover Known Issues](docs/runtime-cutover-known-issues.md)。

## 验证

```powershell
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
docker build --target test -t metaagent-test .
docker run --rm metaagent-test
```

PostgreSQL 集成测试需显式设置 `META_AGENT_TEST_POSTGRES_DSN`，仅接受名为 `metaagent_test` 的独立测试库。
未提供时属于 environment-dependent skip，不视为 failure。Docker 内运行该测试时还需连接测试数据库所在网络。

2026-10-08 本机 Docker 迁移验证结果：

- 完整 Python 回归：152 passed / 0 failed / 0 skipped，包含独立 `metaagent_test` 数据库的 PostgreSQL 集成测试。
- `ruff check src tests` 和 `ruff format --check src tests` 均通过。
- 容器内三层患者直连查询及 IReTour 历史查询均成功；多源接口和两项 IReTour 报表调用保持禁用。
- 已发布 Dify WebApp 完成 1 次单轮冒烟和 2 次连续追问：2 succeeded / 1 partial / 0 hard failures。
  `partial` 来自该演示患者没有 IReTour 可用结果，IReGo 分析和原始报表仍成功生成。
- 重启 MetaAgent 后，同一运行的记录保持一致，后续连续追问成功。
- 结果保存在本机 Excel，原始 PNG 按字节保留并嵌入工作簿；仅检查文本、HTTP 和文件字节，未做视觉复核或医学正确性评价。

测试资产位于已忽略的 `outputs/20261008-docker-cutover/`。Runtime Cutover 的历史收口验证见
[Runtime Cutover Merge Readiness](docs/runtime-cutover-merge-readiness.md)。

## 工具与轻量知识库接入

医疗工具统一入口在 `src/meta_agent/medical_tools/`（2026-11 从 `metaAgent-dev` 的 `medical_tools`
设计合并回主线，实现以主线为准）：

```text
RunContext.call
    ↓ MedicalToolGateway（注册表校验：未知工具 / 功能开关）
    ├── AIWebApiAdapter      → .NET AI_WebApi（患者、康复、量表、报表、医院运营）
    ├── platform/scene.py    → 中台点位库（场景指令，本地匹配）
    └── platform/doctors.py  → 中台在线医生（房间外挂号名单，/api/users）
```

`registry.py` 是工具名、参数、功能开关与提供方的唯一来源，`tools/ai_webapi.py` 与
`application/context.py` 都从这里取端点集合，不再各存一份。`dry_run` 时一律走 AI_WebApi
适配器返回合成数据；注入合成后端（测试）时场景/医生回落到该后端的 `post`，
真实客户端才接本地中台，行为与合并前一致。

在 master `0620db1` 基础上新增 IReCog、线上量表各 6 个工具；两类工具使用可信 project 患者身份，
报表分别由 `META_AGENT__IRECOG_REPORTS_ENABLED`、`META_AGENT__ASSESSMENT_REPORTS_ENABLED` 显式开启。

知识检索支持最新 `knowledge-search-v1` 统一接口，通过 `.env` 的
`META_AGENT__KNOWLEDGE_SOURCES` 配置多个服务、领域和凭据，不在 MetaAgent 中运行索引、重排或厂商检索代码。
配置示例、接口边界和迁移范围见 [工具与知识库迁移说明](docs/tool-and-knowledge-migration.md)。

原生请求继续使用必填 `request_id` 及原生运行快照；`interactive.py` 不带编号的请求已由服务端兼容，
返回 `answer`、`task_id` 和会话 ID，支持项目患者编号、手机号及 `-v` 状态查询。
协议分流、流式响应与范围见 [交互聊天接口兼容](docs/interactive-chat-compat.md)。
