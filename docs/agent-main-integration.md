# Agent / main 整合接入说明（2026-11-03）

合并来源：agent `70b547ba`、main `81bdd477`。目标分支为 agent；main/dev 不随本次更新。
本文件描述代码行为及接入条件，不表示已切换真实部署。

## 入口与响应

优先使用 `POST /v1/chat-messages` 或 `/compat/dify/v1/chat-messages`。
`/v1/chat` 保留 main 的请求格式区分：包含 request_id 时是原生协议，否则是 Dify 文本协议。
所有入口均保留服务认证；网关必须验证并注入真实的 user、租户、角色，不能把服务密钥公开给不可信客户端。

`inputs.pos` 包含**连续的“问诊房间”**才进入问诊 Flow。“房间A”“问诊区附近的康复房间”均进入普通 Flow。
问诊每轮传非空 pos。缺少位置的问诊续聊返回上下文错误，不回退普通流程；问诊 Flow 缺失也不回退。

```json
{
  "user": "verified-actor",
  "query": "帮我填写病历",
  "conversation_id": "",
  "inputs": {"pos": "三楼问诊房间A", "role": "clinician"},
  "response_mode": "streaming"
}
```

首次留空 conversation_id。前端保存响应中的实际 ID，后续每轮传回。
普通 A 带入问诊会创建 B；B 带回普通位置会恢复来源 A（仍有效且授权范围一致），否则创建普通会话。
从 A 再发起新问诊创建 B2；明确携带有效 B 才恢复该问诊。不同房间位置、consultation_id 或已授权目标患者会新建会话。
若两次就诊的 pos 相同且上游无 consultation_id，后端不能识别物理就诊已更换，前端应明确新建会话。

Dify 不允许用未知 ID 创建会话，未知/跨主体 ID 返回 404。原生协议保留首次自定义短 ID 的兼容行为，
但只创建当前主体的新版本状态，不读取旧归档；切换 Agent 仍返回独立新 ID。

阻塞返回保留 Dify message 外层；Agent/问诊流式保留 workflow/node 生命周期、message、message_end。
临床事件作为 message.answer 内的 JSON **字符串**返回，包含 event、display_text、payload。
病历、医嘱为填表草稿，处方/量表为推荐或选择，结束问诊为确认事件；生成事件不代表已保存、开方或真正结束就诊。
前端按 conversation_id/task_id 归属消息，离开房间后不得把旧请求迟到事件追加到新会话。

场景动作由 AI_WebApi 解析及校验目录、版本、歧义，MetaAgent 不直查场景 SQL。
resolve_scene_instruction 仅预览，navigate_scene 才交付。已校验指令通过首次响应输出一次，重复 request_id 不重放。
交付状态不代表 Unity 执行确认。

## 会话、历史与记忆

ConversationSelector 在加载历史之前选择不可变会话绑定。
状态键包含 v2 版本、授权范围、接入方、Agent 类型、问诊上下文和实际 ID。
历史、agent_messages、previous_category、草稿、候选、当前报告和锁均留在各自会话；索引不能改写 thread_id。
旧状态保留在原键下，不清理、不去重，也不自动注入新 Agent。

`GET /v1/conversations`、`GET /v1/messages` 保留列表/分页接口，可传 tenant_id/role。
携带患者 v2 上下文的查询优先使用 `POST /v1/conversations/query`、`POST /v1/messages/query?conversation_id=...`，
请求体为 `{ "user": "...", "inputs": { ... } }`，避免在 URL 放登录凭据。
列表与消息按完整当前授权范围过滤；临床问诊历史还会重新校验参与关系，撤权后不可读。

当前已提交代码仍没有完整长期 MemoryService。本次没有合入其他工作区的未提交原型，
没有把 ConversationState 冒充长期 memory。两个 Agent 共享同一已验证用户的长期偏好属于后续独立阶段；
应共用用户 owner key，不能共享会话原文或合并不同医生/患者的记忆。

## 真实 Dify 工作流桥接

若前端先进入真正的 Dify，再由 HTTP 节点调用 MetaAgent，外层 Dify 的 ID 不会因 HTTP 节点返回新 ID 自动切换。
同一 Dify 应用/凭证可以分别建立普通、问诊两段外层 conversation，前端按场景保存 ID 和会话变量。
桥接每轮传当前非空 pos，并传成对的 `inputs.source_app` 与 `inputs.caller_conversation_id`。
后端按已验证主体、接入方、应用、外层会话、Agent 类型、目标患者/问诊上下文稳定映射独立内部 ID。
更换患者或新的就诊上下文应传新的 consultation_id 或新建外层会话。

只启用内部映射不能保证外层 Dify 的历史和变量隔离。此次测试覆盖 ASGI 兼容 API 及桥接映射，
没有访问真实 Dify WebApp、修改前端或部署工作流；实际外层切换仍须接入验收。

## 临床授权依赖

保留问诊分类及原有业务处理文件，暂不把临床工具整体迁往 AI_WebApi。
main 的患者 2.0.0 契约、patient_ref、scope_fingerprint、data_scope、机构范围及报表来源快照校验保留。
旧 patientId/projectPatientId/患者手机号不能替代登录凭据。

服务 Bearer 和 pos 不证明某医生/患者参与本次问诊。启动装配应注入
`application.consultation_identity_provider`，实现异步 `authorize(scope, category)` 返回 `ConsultationAccess`。
这是服务端依赖，不能由 inputs 传入；本仓库未臆造真实业务授权接口，因此默认未装配。
未装配时普通能力、问诊基础问答/场景指令及结束确认可用；涉及患者资料的五个临床分支明确返回 unavailable。

provider 接收已认证 scope 的 actor/role/client_id、position、consultation_id 和上下文。
先以 category=session 验证参与关系并返回真实 patient_ref，**在选择会话及读取历史之前**完成目标患者绑定；
之后按实际临床类别重新确认权限，历史查询使用 category=history。
返回字段包括 authorization_scope、consultation_context、patient_ref、requester_phone、patient_phone、allowed_categories。
allowed_categories 必须明确包含对应类别（包括 session/history）；作用域、上下文、目标患者每次都须一致。
联系方式只留运行期，不存 inputs、模型上下文或日志。可信 provider 的真实实现需依据业务登录及参与关系契约补齐。

临床登录/读取/详情请求共享工具预算、并发限制和取消，疾病检索词选择使用已配置模型及 LLM 预算，
不再使用独立 DeepSeek 凭据或关闭 TLS 校验的同步网络调用。
知识问答统一 search_knowledge/MaxKB-RAG，删除独立企业问答；knowledge_backend 使用 maxkb。
处方与量表选择只能来自本问诊已推荐 ID。评估分析尚无独立处理器，明确返回 unsupported。

## 验证

离线检查：`uv run --locked --extra dev pytest --ignore=tests/integration -ra`；
静态检查：`uv run --locked ruff check src tests interactive.py`。
离线测试拦截非本机真实网络，覆盖真实分类/SingleAgentRuntime/处理器、Dify 两入口与两种响应方式、
普通↔问诊历史隔离、医生切换目标患者、临床撤权历史、候选选择、临床预算/取消和场景动作不重放。
原本地场景 SQL/匹配测试改为 AI_WebApi 来源、版本、歧义、交付契约测试；临床接口使用合成响应。
PostgreSQL 实例测试需要独立测试 DSN；真实问诊授权、真实外层 Dify 与临床持久化不在离线通过声明内。
