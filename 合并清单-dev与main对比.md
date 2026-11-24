# dev 与 main 代码合并清单

> 生成时间：2026-11-24 15:05
> dev：`metaAgent-dev/src/meta_agent`（2026-11-23「角色/能力/资源/记忆」授权重构线，152 个 .py）
> main：`src/meta_agent`（当前运行线，含 11-16 处方超时修复、11-20 MaxKB live 改造、11-24 免签身份改造，150 个 .py）

---

## 一、核心结论（先看这个）

**这两份代码不是「新旧版本」，而是两条独立演化的版本线，不存在逐文件直接覆盖的关系。**

- dev 的本质：一整套**角色授权体系**横切了整条代码库——新增 `roles/`、`orchestration/capabilities.py`、`orchestration/resources.py`、`agent/role_context.py`、`memory/*`，并把 `TrustedScope` 扩展出 `resource_grant`、把网关改成能力校验。
- main 的本质：在旧架构上做了**三个真实 bug 修复/改造**——处方超时修复、MaxKB live 检索、患者身份免签通道。
- 致命事实：dev 依赖的底座模块（`roles/`、`capabilities.py`、`resources.py`、`memory/`、`role_context.py`）**在 main 中全部不存在**；main 特有的 `resource_grant`、`hospital_ops_allowed`、`consultation_intent_llm`、`consultation_medical_kb`、`owner_aliases` 等符号**在 dev 中全部缺失**（均已实测核实）。

因此：**任何一个 dev 文件单独覆盖到 main，都会直接 ImportError / AttributeError，或回退掉一个已修复的 bug。**

### 必须先做的决策

| 路径 | 说明 | 代价 |
|---|---|---|
| **路径 1（推荐·保守）** | 保留 main 运行线整体不动，只挑拣 dev 的**低风险增量**（1 个 B + 约 8 个低耦合 C） | 不引入角色授权体系，dev 的大部分工作不落地 |
| **路径 2（整体切换）** | 把 dev 的角色授权体系**整包**移植到 main：`roles/`+`capabilities.py`+`resources.py`+`memory/*`+`role_context.py`+ 10 个强耦合 D 文件 | 回滚免签通道、关闭 LLM 分诊、MaxKB 回退到快照版——属于重大改版，需重新联调 8098/8099 |

下面清单同时服务两条路径：`动作`列中的 A/B/C/D 含义见第二节。

---

## 二、动作定义

- **A｜保留 main**：dev 版本会回退 bug 修复或无实质改动，不动它。
- **B｜dev 覆盖**：dev 是纯增量、依赖齐全，可直接覆盖。
- **C｜人工挑拣**：只合并 dev 的特定片段，保留 main 的冲突逻辑（清单标出冲突段）。
- **D｜暂缓决策**：属于角色授权体系核心，要么随路径 2 整包切换，要么维持 main。

---

## 三、57 个差异文件的完整决策矩阵

统计：A=9、B=1、C=30、D=17

### A｜保留 main（9 个，禁止用 dev 覆盖）

| 文件 | 原因 |
|---|---|
| `flows/ai_prescription/webyun.py` | **最高优先级**：dev 回退了 11-16 处方超时修复——把 `GetPrescriptionListNew` 退回 `GetPrescriptionList`（中文 keyword 挂死）、删掉 `TimeoutError` 降级、删掉 online/offline 分组兼容与 `Hospitals` 字段 |
| `flows/ai_prescription/render.py` | dev 删掉 `build_schedule_info` 排班反建，会导致线下时段选择失效 |
| `flows/ai_prescription/slots.py` | 同上，与 render.py 配套 |
| `flows/ai_consultation/__init__.py` | dev 仅 24 行空壳（继承 `ConsultationRoomFlow`，无 `_pipeline`）；main 251 行是 11-18 完整问诊链路（意图识别/分诊/挂号/知识问答），覆盖即全丢 |
| `domains/scene_instruction.py` | 纯 CRLF/LF 行尾差异，内容一致，无需改动 |
| `medical_tools/__init__.py` | 纯行尾差异 |
| `medical_tools/adapters/platform/doctors.py` | 纯行尾差异 |
| `medical_tools/adapters/platform/scene.py` | 纯行尾差异（601 行一致） |
| `tools/ai_webapi.py` | 纯行尾差异（366 行一致） |

### B｜可直接用 dev 覆盖（1 个）

| 文件 | dev 的增量 | 依赖核对 |
|---|---|---|
| `events/dify.py` | 新增 `history_text()`：clinical 分支从 answer_part 的 JSON 取 `display_text` 经 `model_text` 脱敏后作历史文本，替代原文 `event.payload["text"]` | `model_text`、`CLINICAL_BRANCHES` 在 main 均存在，可安全覆盖 |

### C｜人工挑拣（30 个）

**C-1 低风险、推荐在路径 1 下挑拣（8 个）**

| 文件 | 挑拣内容 | 冲突段/注意 |
|---|---|---|
| `observability/logging.py` | `SAFE_FIELDS` 白名单新增 `role/agent_id/profile_version/flow/capability/decision` | 纯放宽，不记录患者原始数据 |
| `infrastructure/migrate.py` | 新增 `PostgresPreferenceRepository.setup()`（偏好表） | 仅 `patient_memory_enabled`+postgres 时必需；单独合入无害 |
| `api/security.py` | 新增 operator token → `ClientIdentity("backoffice")` | 需同时补 `config.py` 的 `service_bearer_token_operator`，否则 AttributeError |
| `config.py` | 叠加 dev 新增项（`service_bearer_token_operator`、`explicit_role_clients`、`resource_authorization_*`、`patient_memory_*` 等） | **必须保留 main 的 `consultation_intent_llm=True` 和 `consultation_medical_kb=True`**，覆盖会导致 LLM 分诊被静默关闭；`hospital_ops_open_to_all_roles` 默认 True→False 属策略变更，需确认 |
| `medical_tools/contracts.py` | 新增 `HOSPITAL_METRICS`（五组指标）、`ToolContext.excluded_tools` 可选字段 | 纯增量扩展，未删 main 任何符号，合入不改变行为 |
| `flows/consultation_room/medical_record.py` | **单独采纳 dev 的响应体脱敏**（删 `body_preview` 日志） | 其余「取数源替换」不要合——dev 删掉了 phone 取数的降级容错 |
| `flows/default/post_process.py` | **单独采纳 dev 的 timestamp 正则校验**（`\d{1,20}(?:\.\d{1,9})?`）与**最终 answer 不再写日志** | 不要合 `build_chat_history_url(role=...)`，会改前端契约 |
| `tools/demo.py` | 合成响应补 `scope`/`section`/`hospital_id` | 向后兼容；仅在做机构授权时需要 |

**C-2 中风险、需保留 main 业务流（20 个）**

这些文件 dev 的改动主要是两类：（a）把 phone 取数替换为 `encounter_summary()`；（b）注入 `require_for_run`/`denial()` 能力门禁。合并原则是**保留 main 的取数与降级逻辑，只挑门禁**（但门禁本身依赖 D 类的 capabilities 底座，未引入前不能落）。

`agent/context.py`、`application/legacy_conversations.py`、`flows/__init__.py`、`flows/ai_prescription/__init__.py`、`flows/ai_realtime_analysis/__init__.py`、`flows/ai_summary/__init__.py`、`flows/consultation_room/__init__.py`、`flows/consultation_room/analysis.py`、`flows/consultation_room/invite_doctor.py`、`flows/consultation_room/medical_order.py`、`flows/consultation_room/prescription.py`、`flows/consultation_room/scale_assessment.py`、`flows/consultation_room/subtasks.py`、`domains/chat_history.py`、`domains/doctors.py`、`domains/hospital.py`、`domains/portfolio.py`、`domains/remote_knowledge.py`、`domains/scene.py`、`planning/parser.py`

其中三个要特别记住：

- `planning/parser.py`：dev 新增 `hospital_plan()`/`operator_decision()` 可挑拣，但**dev 删掉了 main 11-24 的「步道」中文别名修复**（`explicit_tour_cn` + 提示词两行），挑拣时必须保住 main 这部分。
- `domains/hospital.py`：dev 删掉了 main 的 `RENDER_FIELDS` 与中文摘要生成能力；只可挑 `urlsplit().path` 报表 URL 校验。
- `domains/chat_history.py`：dev 给 URL 加 `&role=` 参数，前端未支持前不要合。

**C-3 工具集成，需底座（2 个）**

| 文件 | 说明 |
|---|---|
| `integrations/knowledge.py` | dev 新增角色过滤（`sources_for_role/scope`、`search(role)`），但把 main 的 live 配置（`catalog_path/sha256` 可缺省、`owner_aliases`）改成必填并删除映射——冲突段在 `NativeMaxKBConfig` 字段定义与 `search()` 签名。保 live 则不动 |
| `medical_tools/adapters/ai_webapi.py` | dev 127 行 vs main 24 行：**main 确实是骨架，dev 是真实实现**（患者身份注入/封套校验、机构授权裁剪、`scoped_hospital_response`）。可挑拣「患者注入/封套校验」段（依赖齐全、低风险）；机构裁剪段需连同 `HOSPITAL_METRICS`+gateway 一起迁 |

### D｜暂缓决策（17 个，角色授权体系核心）

| 文件 | 冲突本质 |
|---|---|
| `orchestration/identity.py` | **最关键**：dev 强制 `identity_context_token`、删除 `requesterphone`→`login_name` 映射、删除 `hospital_ops_allowed`。用 dev 覆盖 = 11-24 免签改造整体回滚 → `scope_for` 阶段 422、`has_patient=False`、患者工具全禁、`patient_payload` 抛 `patient_required`，**8099 自动签发流程整体不可用** |
| `api/routes.py` | 与 identity 同一闭环；dev 改走 `resolve_patient_identity()` 失败不再降级，覆盖会切断免签患者解析 |
| `agent/runtime.py` | 换成 `RoleContextMiddleware`、去掉 `[Answer:]` 前缀——main 的 `api/routes.py` 分段依赖该前缀 |
| `agent/tools.py` | 权限模型整体替换为 capabilities 白名单；dev 依赖缺失模块，覆盖即 ImportError |
| `application/context.py` | 网关调用契约整体改变 |
| `application/conversations.py` | dev 删 `token/webyuntoken` 别名，会破坏 main 的 `ai_prescription`（新建处方传 webyun token） |
| `application/service.py` | 分支绑定方式与 main 互斥 |
| `infrastructure/container.py` | 注入 memory 服务，强依赖 `memory/*` |
| `flows/consultation_room/client.py` | 契约级重构：`__init__(ctx, capability)` 必填能力位，所有调用方联动 |
| `flows/default/__init__.py` | 依赖 `Profile` 契约 |
| `flows/fixed_task.py` | `FIXED_CLINICAL_CAPABILITIES[branch]`，影响所有固定任务 |
| `domains/knowledge.py` | 给 `Document` 加 `roles` 字段，旧语料无此字段（默认空集 → 全部查不到），需数据迁移配套 |
| `domains/routing.py` | 角色体系入口，`route_from_inputs(..., role=)` + `ROLE_AGENTS.for_role(role).flows` 校验，深度耦合 |
| `planning/compiler.py` | 能力门禁贯穿编译与校验两阶段 |
| `integrations/maxkb_native.py` | **路线级互斥**：dev 回退 11-20 live 改造，改回 catalog 快照检索（`schema_version==2`+sha256），丢失实时列库/`owner_aliases`/正文哈希 revision，且每次命中多 2 次 GET |
| `execution/dispatcher.py` | 加 `require_for_run`，依赖 capabilities |
| `medical_tools/gateway.py` | 强制 TrustedScope + 能力/空间/机构授权，执行逻辑与所有签名整体替换 |

---

## 四、dev 独有模块（10 个）与 main 独有模块（8 个）

**dev 独有（路径 2 的前提，main 全部缺失）**
`agent/role_context.py`、`roles/__init__.py`、`roles/prompts.py`、`orchestration/capabilities.py`、`orchestration/resources.py`、`memory/context.py`、`memory/postgres.py`、`memory/repository.py`、`memory/schemas.py`、`memory/service.py`

**main 独有（dev 缺失，切勿被覆盖掉）**
`flows/ai_consultation/` 下的 `booking.py`、`diagnosis.py`、`error_code.py`、`intents.py`、`knowledge.py`、`llm_intent.py`、`prompts.py`、`question_classify.py` —— 这是 11-18 完整 AI 问诊链路的 8 个拆分模块。

---

## 五、推荐执行步骤（路径 1：保住 main）

按此顺序操作，每步做完跑一次验证：

1. **先建立基线**：备份 `src/meta_agent`，确认 8098/8099 当前可用（实测 `GET /lab` 返回 200）。
2. **直接覆盖 1 个文件**：`events/dify.py`（B 类，纯增量）。
3. **挑拣 4 处独立安全/兼容改进**：
   - `flows/consultation_room/medical_record.py`：只删 `body_preview` 日志；
   - `flows/default/post_process.py`：只加 timestamp 正则、去掉 answer 日志；
   - `medical_tools/contracts.py`：加 `HOSPITAL_METRICS` 与 `excluded_tools` 字段；
   - `config.py`：叠加新增项，但**保留** `consultation_intent_llm` / `consultation_medical_kb`。
   （`api/security.py` 和 `tools/demo.py` 只在引入 operator/机构授权时再合。）
4. **`medical_tools/adapters/ai_webapi.py`**：挑拣患者身份注入与 `validate_patient_envelope` 段（dev 有、main 骨架缺），这是 main 的真实功能缺口，低风险。
5. **明确不动**：所有 A 类 9 个、所有 D 类 17 个文件。`flows/ai_prescription/webyun.py` 与各 D 文件加一行注释说明为何保留 main，防止后续误覆盖。

**验证清单**
- 8099：`requesterphone=15800000001` + 「我是谁」→ 自动注入 `patient_identity.value=5872`、工具 succeeded；
- 8099：「查下肢报告」/「最近一次的量表」→ 工具真实调用，返回 fail-closed 兜底而非报错；
- 8098：AI 推荐处方 → 不再出现「本次请求已达到执行时限」；
- 8098：知识类问答 → `search_knowledge` 命中电信服 MaxKB live 库；
- 回归：`planning/parser.py` 问「看步道报告」→ 走 iretour 而非场景查询。

---

## 六、如果选路径 2（整体切换）

必须一次性整包迁移，不能分批：

`roles/`(2) + `orchestration/capabilities.py` + `orchestration/resources.py` + `agent/role_context.py` + `memory/*`(5) + D 类 17 个文件 + C-2/C-3 的门禁片段 + config 全量替换。

**切换前必须接受的后果**：免签通道回滚（前端必须传完整 v2 凭证）、LLM 语义分诊被关闭、MaxKB 回退快照版（知识库更新需重新生成 catalog）、患者工具权限模型全变、8098/8099 需重新联调。

---

*本清单基于 57 个差异文件的逐段 diff 与依赖实测；未修改任何源码。*

---

## 七、实际合并后验证结果（2026-11-24 16:10）

用户选定“以 dev 为主”，实际已执行：dev 全量覆盖 `src/meta_agent`，补回 main 的 `ai_consultation/` 子模块与 11-20 live 实时检索链。验证结论：

- 编译/导入：`compileall` 通过；158 个模块导入 0 失败；`ruff check src/meta_agent` 全绿。
- 角色与 live 兼容修复：恢复 dev 的 `sources_for_scope` / `role` 传参；live 库名角色改为按传入角色过滤（未传时保持 operator 兼容）。MockTransport 实测 patient 命中、clinician 不命中。
- `ConsultationClient` 的 `capability` 改为可选，仅兼容补入的 ai_consultation 病史取数；dev 既有显式授权调用点不变。
- 依赖：`langmem>=0.0.30,<0.1` 已安装，`pyproject.toml` 与 `uv.lock` 已同步；此前 E501 提示通过补 `roles/prompts.py` 忽略项解决。
- 测试：main 的 `tests/test_ai_consultation_flow.py` 40/40 通过；dev 测试集除 14 个旧 catalog 模式 MaxKB 用例和 1 个缺 emitter 的过期 ai_consultation 用例外全部通过（这 15 个失败是测试集版本不匹配，不是运行时故障）。
- 服务：8098、8099 `/lab/clients` 均 HTTP 200；8099 已带患者 v2 签名 key 重启，自动注入 `patient_identity.value=5872` 生效。模拟模式端到端 `ai_consultation` succeeded。
- 真实模型端到端被外部模型账户余额拦截：LLM 返回 402 `Insufficient Balance`，非代码/合并问题；备好额度后按同一命令重跑即可。
