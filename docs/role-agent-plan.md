# MetaAgent 四角色 Agent 方案

**决策（2026-11-23）：** 前台有患者、医生、学生三个入口，后台保留医院运营人员。一个账号可以具备多个身份，但每轮只有一个由网关验证的活动角色。本期不设计家属代办，也不把“未绑定患者”当成学生。

## 1. 本次已接入：学生身份与学习模式

| 入口 | 可信网关传入 | 生效的 `role` |
| --- | --- | --- |
| 患者 | `role=patient` 或 `identity=患者/patient` | `patient` |
| 医生 | `role=clinician` 或 `identity=医生/doctor/clinician` | `clinician` |
| 学生 | `role=student` 或 `identity=学生/student` | `student` |
| 运营后台 | 后台鉴权后显式传 `role=operator` | `operator` |

这里的 `/` 表示多个可选拼写。前台网关须发送已验证的角色，服务令牌由网关保管。显式 `role` 优先于兼容字段 `identity`。漏传、空值或不受支持的身份默认返回 422。旧患者专用网关可显式启用缺省身份兼容；读取本人档案仍需患者 v2 凭证。后台网关须核验运营资格。

学生请求示例（已有聊天接口和响应格式不变）：

```json
{"user":"verified-user","query":"请就这段教材出一道复习题","inputs":{"identity":"学生"},"response_mode":"blocking"}
```

- 学生使用学习提示词和独立会话范围，不使用患者口吻、患者偏好或患者数据工具。进入问诊房间也留在学习模式。没有教学模型时返回不可用，不改走旧患者规划器。
- 学生可基于自带的、不含个人信息的教材练习。查资料只使用原生 MaxKB；当前空间、已发布的知识库路由和文档必须同时匹配，其中路由和文档都须授权 `student`。没有证据就说明缺口，不转向运营资料、旧桥接或本地通用语料。接入身份不会自动发布教材。
- 通过现有会话/消息 API 读取本角色历史；前端在聊天、续聊、历史、运行状态与取消请求中保持同一角色。切换角色时不要沿用上一角色的会话 ID。历史上误归入患者范围的会话不自动搬给学生。
- 初次学生接入后已落实四份运行策略。原生 MaxKB 不再把其他角色映射成运营；运营共享开关的兼容例外和严格部署配置见第 4 节。


### 功能令牌和活动角色

接入网关用服务令牌选择客户端档案。固定临床流程还须传已核验的活动角色。服务端先匹配角色允许的流程，再按本次就诊授权开放临床操作。客户端工具白名单和排除列表同时约束模型和工具执行端；各令牌继续使用自己的日志。

| 令牌配置的流程 | 可用角色 | 就诊授权能力 |
| --- | --- | --- |
| `ai_consultation`、`consultation_room` | 医生、患者 | 基础问答可直接使用。其他子任务按角色和本次授权开放 |
| `ai_summary` | 医生 | `clinical.summary` |
| `ai_medical_order` | 医生 | `clinical.order` |
| `ai_prescription` | 医生 | `clinical.prescription`。启用自动建模板时，写入另需 `clinical.prescription.create` |
| `ai_realtime_analysis` | 医生 | `clinical.analysis` |

医生在普通入口按 `pos` 切换问诊内外。专用令牌固定流程，可以在没有房间位置的独立应用中提交 `consultation_id` 和 `resource_context_token`；明确传入问诊外位置时，临床操作会被拒绝。纪要可以使用本轮提交的对话、授权方返回的本次就诊摘要或当前会话。处方应用兼容的上游令牌只在本轮内存中传递。

## 2. 四个 Agent 的目标方案

| 角色 Agent | 首个用户任务与处理方式 | 数据和动作边界 | 验收例 |
| --- | --- | --- | --- |
| 患者 · 康复助手 | “我上次训练怎么样？”绑定本人后，查训练记录，用通俗语言解释，并区分已记录的医嘱与一般建议。 | `get_patient_*`、设备记录和本人报表；医生查询、空间导航按接入方配置。长期只记允许的非医疗偏好，不查家人的档案。 | 未绑定时仍能看公开指引；问本人训练时提示绑定，不把缺记录说成没训练。 |
| 医生 · 问诊助手 | “整理这次问诊的病历和医嘱。”以当前就诊和已授权的目标患者为范围，标注资料来源，返回待医生复核的草稿。 | 医生账号不是目标患者；查目标患者须由业务后端校验就诊关系。推荐、草稿、确认、实际提交分别处理，不能仅凭房间位置或姓名获得权限。 | 切换患者后不带入上一人的草稿；未取得授权时不读取病历；草稿不显示为已提交。 |
| 学生 · 学习助手 | “解释这个概念，再考我一道题。”先解释，再用明确标注的虚构案例提问，根据作答给反馈，并标明教材出处。 | 仅学生获准的教材、空间说明和本角色学习对话；不查真实患者、不参与真实问诊、不挂号、不下发界面动作、不查运营。学习进度如要持久化，另设学习记录，不写患者记忆。 | 同一学生在大厅与问诊房间都能继续同一学习对话；没有学生版教材时不引用内部资料。 |
| 运营 · 机构分析助手 | “汇总上月本机构门诊量。”确认授权机构和期间，再查指标，注明统计口径、覆盖和报表来源。 | 以 `query_hospital_directory`、`query_hospital_operations` 为核心，只读机构范围内的汇总。运营身份不是平台管理员，也不天然获得个人病历权限。 | 切换机构重新核验范围；没有数据不补零；用户未要求报表时不额外生成制品。 |

## 3. 已实现的运行链

可信网关验证活动身份 → `RoleAgentRegistry` 选中版本化方案 → 选择方案允许的场景 → `ConversationSelector` 绑定后读取历史 → 共用运行时 → 能力与资源校验 → 适配器 / 业务服务。`agent_id` 标识方案；旧字段 `agent_kind` 仍只表示 `default` 或 `consultation_room`。不由模型切换身份，不新增聊天端点或建四套存储。

| 模块（相对 `src/meta_agent/`） | 职责 |
| --- | --- |
| `roles/`、`agent/runtime.py`、`flows/default/` | 患者、医生、学生、运营分别使用 `patient_rehab`、`clinician_workspace`、`student_tutor`、`hospital_ops`。共享模型和预算，但能力上限、提示词、场景、记忆和无模型策略独立。无模型时学生和医生助手明确不可用；运营仅有受限的机构指标规则查询，不运行患者规划器。只有已绑定患者可用本人长期偏好。 |
| `api/`、`application/conversations.py`、`application/service.py` | 服务凭证区分前台与后台；授权指纹隔离聊天、续聊、历史、幂等、状态与取消。不同角色、就诊目标、机构或授权集合不共用数据。凭证续期或只升级提示词不改变同一授权范围。原生与 Dify 保留阻塞/流式和会话 ID 契约；重查已交付的临床事件只返回展示文本。 |
| `orchestration/capabilities.py`、`medical_tools/`、`execution/`、`flows/consultation_room/` | 同一能力策略控制模型工具、计划校验、实际执行、问诊处理器、医疗网关和动作交付；未授权时不读取业务数据。问诊中的非传送点位及医生的非传送操作仅可执行按空间/版本核准的公共控件；临床草稿和确认不等于已提交。 |
| `integrations/knowledge.py`、`integrations/maxkb_native.py`、`domains/knowledge.py` | 原生 MaxKB 使用实际角色，同时核验空间、知识路由和文档。发布清单原来只有 `operator` 的文档不会自动对前台开放；教学资料需要明确发布为 `student`。 |
| `orchestration/resources.py` | 受信业务方在线核验操作者与目标之间的关系；短期授权不从浏览器布尔值、手机号、机构编号或会话 ID 推断。 |

## 4. 接入与切换

### 4.1 前台、后台和历史页

- 前台沿用原服务凭证，网关先验证账号确有该身份，再注入 `user / tenant_id / role`。默认 `META_AGENT__EXPLICIT_ROLE_CLIENTS=["primary","secondary"]`。只有经过确认且仍需过渡的旧患者专用网关才应从该列表移除，兼容缺省身份。
- 运营后台使用**单独**的 `META_AGENT__SERVICE_BEARER_TOKEN_OPERATOR` 和显式 `role=operator`；没配置就不开该入口。前台凭证不能选择运营，后台凭证也不能代扮前台身份，两种情形都返回 403。服务凭证只证明网关，不替网关核验账户角色。
- 历史 GET 必须沿用本次角色和知识空间（`space_id`）；不会再从仅有的用户或会话 ID 反推其他角色。涉及患者 v2、就诊或机构数据，使用 `POST /v1/conversations/query` 和 `POST /v1/messages/query`，传与聊天相同的身份、资源和凭证；运行状态与取消同理。后台凭证和资源令牌不得放入 URL 或发布给浏览器。
- 旧普通会话在归属与授权仍可验证时可保留；旧未核验就诊的绑定、历史和运行不会恢复。不自动把旧患者历史搬给学生。提示词更新不需要全库迁移；授权范围有变动则重新开始相应范围的会话。

### 4.2 业务授权契约

由业务方实现并配置 `META_AGENT__RESOURCE_AUTHORIZATION_URL`（HTTPS）和 `META_AGENT__RESOURCE_AUTHORIZATION_TOKEN`（独立服务凭证）。**本仓库实现校验客户端，不签发授权，也不提供真实授权服务器。**

- 医生网关每个受限请求附 `inputs.consultation_id` 与 `inputs.resource_context_token`；患者参与特定就诊操作还需本人 v2 上下文。运营请求附 `inputs.current_business_id` 和 `inputs.resource_context_token`。
- MetaAgent 发 HTTPS POST；请求体字段为 `user, tenant_id, role, client_id, consultation_id, hospital_id, resource_context_token`（非对应目标为空字符串或 null）。校验方必须验证凭证、当前主体、角色和目标关系，而非直接回显输入。
- 成功响应是一个 JSON 对象：必须回传匹配的 `user, tenant_id, role, client_id, capabilities, expires_at`（Unix 秒）。就诊授权还需 `consultation_id, patient_ref, patient_phone, requester_phone`，可带仅限这一就诊的 `encounter_summary`；不会再按请求人电话拉取全量聊天记录。可授予的就诊能力为 `clinical.record/order/prescription/scale/invite/end`（每个值带 `clinical.` 前缀），患者最多 `clinical.invite` 和 `clinical.end`。旧手机号字段即使保留，也不能覆盖已核验的目标。
- 机构授权回传 `hospital_id` 和 `hospital.directory`、`hospital.metrics` 中的相应能力。查询指标还需非空 `sections`（如 `["rooms","consultation"]`，显式 `["all"]` 才表示全部）；只接受本机构，拒绝平台或跨机构比较，分项授权不生成综合报表。上游返回若不能确认同一机构 ID、范围和指标，不进入模型或证据库。
- 未传资源凭证仍能普通问答，但不能使用相应受限能力；传了无效或不匹配的授权返回 403，校验方未配置、故障或返回不合法响应则为 503，不降级到电话查询。每个带凭证的请求（包括重放/历史/状态）重新校验；运行内再次检查能力和过期时间。换凭证但同一有效范围可续聊。

### 4.3 发布与回退

1. 先联调一个账号切换患者/学生，以及同一医生换就诊、同一运营人员换机构。检查 `agent_role / agent_id / profile_version / branch`，在实际授权服务上线前保持受限能力不可用；签名、病例原文不写入授权日志。
2. 复核并重新发布 MaxKB 目录和文档受众，再更新部署的校验摘要。不自动重标任何现网资料。旧 HTTP 知识源没有文档 ACL，只可为经审定的整库配置 `knowledge_sources[].roles`；本地语料需逐文档 `roles`，Portfolio 整库需 `META_AGENT__PORTFOLIO_ROLES`。这些旧来源均不可用于学生。非原生来源调整发布权限时同步更新 `META_AGENT__KNOWLEDGE_ACCESS_REVISION`，避免用旧对话恢复受限摘录。
3. 运营指标默认只对后台开放。升级时清除既有部署的 `META_AGENT__HOSPITAL_OPS_OPEN_TO_ALL_ROLES=true` 覆盖，或将其设为 `false`。如前台需要公开机构目录，可独立设置 `META_AGENT__HOSPITAL_DIRECTORY_PUBLIC=true`。通用问诊/医生控件按已复核目录配置 `META_AGENT__PUBLIC_SCENE_CONTROLS`，例如 `{"示例空间":{"7":["[ClickPoint:]0"]}}`。
4. 按角色灰度；需要停用某方案时设 `META_AGENT__DISABLED_AGENT_ROLES=["clinician"]` 等并重载部署。只拒绝该角色新请求，不调用其他角色兜底。仍需外部网关、教学材料、业务授权服务和真实客户端联调后才能上线。
