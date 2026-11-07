# dev 接口规格核查与提交归因

核查日期：2026-11-07。代码基线：`dev` / `origin/dev`，`832bca264bbc753ed119afae7288b255693c7256`。

## 1. 结论

**本轮兼容性变化的集中引入点是 `02ea0ab159b9cc394440da76d5f2c249a6bdd1f1`，Git 作者及提交者均为 `Mrpointdelicious`。** 提交标题为 `Merge main into agent with isolated consultation conversations`。它相对第一父提交 `70b547b` 移除了业务已经使用的 `spaceid` 别名，替换场景解析链路、增加场景版本门槛，并启用了不兼容旧绑定的会话机制。随后 `ea9af92` 将其合入 agent；`ea9af92` 与 `02ea0ab` 的文件树完全相同。当前 dev 包含这条历史。

这里的“谁”指 Git 记录的作者账号，不以提交信息推断实际操作人。Git 历史能证明变更及继承关系，不能证明测试服由谁部署、谁遗漏配置，也不能证明远端 dev 分支指针由谁移动。

适老化合并 `832bca2` 相对测试记录所列基础提交 `2cfc68f` **仅修改 `agent/context.py` 的适老化语气与追问，共增加 15 行、删除 1 行**，没有修改请求字段、路由实现、会话绑定或后端接口。该次 prompt 合并保留了旧导航提示，而工具语义已在 `02ea0ab` 中变化，因此现有提示与工具之间的矛盾仍然存在。

冻结后的业务契约见 [业务接口设计冻结文档](business-api-contract-freeze-v1.md)。本报告只核查和出具文档，没有修改运行代码或部署配置。

## 2. 证据边界

| 证据 | 可确认内容 | 不可据此确认的内容 |
| --- | --- | --- |
| 用户提供的验收工作簿，8 张工作表 | 当次请求、结果摘要、工具名、运行状态、记录的版本与补丁 | 当前在线状态、未展示的完整 SSE 帧、补丁的逐行内容 |
| 用户提供的问题总结 | 当次部署说明、上游错误日志摘录、补丁意图 | 总结中的所有推测、待确认项均已验证 |
| 本地 dev 代码、Git 父提交差异和 blame | 当前行为、改动来源及合并路径 | 测试服镜像与仓库文件逐字一致 |
| 本地离线诊断及现有测试 | 合成输入下的确定性行为 | 真实模型稳定选择工具、前端实际传送、远端凭据有效性 |
| 本地 AI_WebApi 配套代码 `df182bfe49459192745c2fa9fb5bb30486e30b8d` | 场景目录配置、请求方法与契约的实现证据 | 测试服 15043 正在运行该提交 |

没有连接测试服、数据库或中台；附件中的回滚、重启、改配置建议没有作为操作指令执行。

### 2.1 被测版本与当前 dev 的区别

工作簿 `01-环境与版本!B14:B18` 记录分支为 agent、基础提交为 `2cfc68f`，并明确列出 4 个修改文件：`identity.py`、`conversations.py`、`scene_instruction.py`、`scene.py`。问题总结称本地分支为 dev，但同样给出 `2cfc68f`。两份材料的分支称呼不同，基础提交一致。

因此，当次验收对象应表示为 **`2cfc68f + 未提交部署补丁`**，不能表示为干净的 dev，也不能把“打补丁后知识检索通过”解释成“当前仓库已经兼容 `spaceid`”。`01-环境与版本!A30:C35` 给出了补丁与配置调整的对应关系。

`02-测试用例!A2:I12` 共 11 条，9 条通过、2 条失败。其中 T09 的聊天接口是 HTTP 200，内部场景调用失败、运行状态为 `unavailable`；不能把它写成“聊天入口返回 HTTP 502”。T11 才是聊天入口 HTTP 404。

### 2.2 原始附件定位

- 工作簿：`metaAgent-5080-验收测试记录-20261107.xlsx`，SHA-256：`e6eb031cca67ccad33d3e79d53c78fb125d2d74f7153d911614fa44f467237a8`。
- 问题总结：`metaAgent-5080-问题总结-20261107.md`，SHA-256：`af5b612ae714228cf4ae611fface8ffb59a1756e75d9014b97ee2e116d689464`。

文档不复制附件中的凭据、数据库口令或人员联系方式。以下示例均使用占位值。

## 3. 逐项归因

| 编号 | 业务要求或观察 | 当前 dev 行为 | 引入或重新引入的提交 | 判定 |
| --- | --- | --- | --- | --- |
| A01 | 中台发送小写 `inputs.spaceid` | `identity.py:203` 仅识别 `space_id` / `spaceId`；`public_inputs` 白名单也缺 `spaceid` | `29f96d4`（作者 yx）先增加别名；`02ea0ab`（作者 Mrpointdelicious）合并时移除，并新增缺少该别名的白名单 | 确认的字段兼容回归 |
| A02 | T07–T11 均未发送 `sceneVersion`，旧场景实现自行查询版本 | `SceneCommandResolver` 在版本缺失时直接返回 `scene_version_required`，不会调用上游 | `02ea0ab`，Mrpointdelicious | 确认的隐性必填参数变化 |
| A03 | 旧 agent 直接访问场景目录；部署继承旧 `.env` | 新实现调用 AI_WebApi `navigate_scene`，场景目录凭据和地址须配置在 AI_WebApi | `02ea0ab`，Mrpointdelicious | 确认的服务依赖变化；测试服配置缺失来自附件记录 |
| A04 | 业务要求“带我去三楼”产生可执行指令；T09 实际调用 `resolve_scene_instruction` | 该工具现为 `dispatch=False` 的预览；普通场景 SYSTEM_PROMPT 仍引导导航使用它 | `02ea0ab`，Mrpointdelicious，改变工具语义并移除旧 `command` 直接输出 | 确认的语义不一致；配置修好后是否仍失败取决于模型工具选择 |
| A05 | T11 期望旧 `conversation_id` 可续聊 | 只查 `conversation_bindings_v2`，未绑定 Dify ID 在读取旧历史前即 404；thread 哈希也换为 v2 | `02ea0ab`，Mrpointdelicious | 确认存在兼容断点；具体旧会话是否仍在库中需后续核查 |
| A06 | 旧配置 `allowed_space_ids=[]` 可以启动 | `NativeMaxKBConfig` 要求至少 1 个空间 | `2ca551d`（Mrpointdelicious）首次要求非空；`29f96d4`（yx）放宽为空；`02ea0ab`（Mrpointdelicious）恢复非空 | 配置兼容变化；不应为恢复启动而开放所有空间 |
| A07 | T06 只传 `user` 和 `limit` 查询列表，聊天使用 `identity=医生` | GET 列表默认 `role=patient`，查询不到同用户 `clinician` 会话 | `02ea0ab`，Mrpointdelicious，新增 role 参数及含角色/客户端的归属检查 | 离线确认的历史可见性差异；T06 空列表不足以证明业务列表可用 |

### 3.1 为什么不能只看 blame 就归因给 `81bdd47`

`identity.py:203` 的 blame 指向 `81bdd47`，它在 main 上引入了现在的取值表达式。但 agent 后续在 `29f96d4` 中增加了 `spaceid` 支持。**针对“已经支持的业务字段在整合后失效”，决定性证据是 `02ea0ab` 相对第一父提交的差异：**

```diff
- space = _first_value(inputs, ("space_id", "spaceId", "spaceid"))
+ space = inputs.get("space_id", inputs.get("spaceId"))
```

应将 `81bdd47` 记为合并选用的 main 实现来源，将 `02ea0ab` 记为 agent 业务兼容回归的引入点，避免把“文本最早来自哪里”与“哪次整合破坏了现有调用”混为一谈。

### 3.2 场景问题是连续的多层阻断

1. 干净 dev 首先丢失 `spaceid`，空间为空。
2. 修复空间别名后，缺少 `sceneVersion` 又在本地被拦截。
3. 部署补丁放行版本缺失后，请求到达 AI_WebApi，附件记录其缺少 `RehabTools:Scene:VersionUrl`，产生上游 502。
4. 即使目录配置齐全，如果模型仍只调用预览工具，也不会输出 `action_ready`。T09 的工具栏记录为两次 `resolve_scene_instruction`，不能将“上游接口名叫 navigate_scene”误当作“Agent 已调用执行工具”。

第 4 层是本次追加发现：在合成后端返回合法匹配结果时，预览工具返回 `succeeded`，动作事件数仍为 0；调用执行工具 `navigate_scene` 则产生 1 条动作事件。

另外，现有 [系统提示词](https://github.com/Mrpointdelicious/metaAgent/blob/832bca264bbc753ed119afae7288b255693c7256/src/meta_agent/agent/context.py#L27) 与 [业务路由提示](https://github.com/Mrpointdelicious/metaAgent/blob/832bca264bbc753ed119afae7288b255693c7256/src/meta_agent/domains/business_routing.py#L64) 给出了不同工具指引。需要统一执行语义，不能只补后端地址后就宣布导航恢复。

### 3.3 `/back/version` 与 `/back/query` 能确认到什么程度

附件把 POST + `apikey` / `apitoken` 写为待确认推测。旧 agent 的 `70b547b:src/meta_agent/domains/scene_instruction.py` 与本地配套 AI_WebApi 的 `SceneCommandCatalog.cs` 都明确实现：

| 调用 | 方法 | JSON body | 请求头 |
| --- | --- | --- | --- |
| 场景版本地址 | POST | `{"spaceId":"<当前空间>"}` | `apikey`、`apitoken`、`Content-Type: application/json` |
| 场景目录查询地址 | POST | `{"sql":"<服务端固定模板生成的查询>"}` | 同上 |

这是源码层面的确定证据，因此直接 GET 404 不能证明该 POST 契约无效。但真实服务是否仍接受这些头、凭据有效期多久、测试服是否部署此代码，仍未验证。

配套 C# 控制器的 `ExpectedSceneVersion` 是可选值，仅在提供时比较；因此“前端必须传版本”是 MetaAgent 当前解析层增加的门槛，不能归因成已证实的业务侧要求。

### 3.4 旧会话 404 的结论强度

当前代码确实不会在查不到 v2 绑定时尝试旧历史索引。旧 thread 的哈希材料、授权范围和客户端隔离方式也发生变化。这能解释 T11 现象，但附件没有提供旧会话索引、完整归属及 TTL，不能排除过期、不同角色或不同客户端身份等因素。

不应直接采纳附件中“任意未知 conversation_id 自动新建”的建议。冻结设计要求：对能验证归属的旧会话提供兼容映射；确实不存在、过期或不属于当前用户的 ID 继续返回明确错误。新建会话必须返回新 ID，不伪装成已经恢复旧历史。

## 4. 本次离线核查结果

探针采用合成身份、内存仓库和合成场景返回，禁止非本机网络连接；不读取项目 `.env`。

| 探针 | 实际结果 |
| --- | --- |
| 三种空间别名分别解析 | `spaceid → None`；`spaceId / space_id → space` |
| 会话元数据过滤 | `spaceid` 被丢弃，`pos`、`timestamp` 保留 |
| 空间有效、版本缺失，调用执行工具 | `clarification / scene_version_required`；上游调用 0 次 |
| 版本有效、合成后端正常，调用预览工具 | `succeeded`；`action_ready` 为 0 条 |
| 同样输入调用执行工具 | `succeeded`；`action_ready` 为 1 条；交付状态 `delivery_unknown` |
| 未绑定的旧格式会话 ID | HTTP 404，`detail.code=conversation_not_found` |
| 空 ID 创建医生会话 | HTTP 200，返回新 `conversation_v2_…` ID |
| 新建医生会话后，只按 user 查询列表 | HTTP 200，0 条 |
| 同样查询显式传 role=clinician | HTTP 200，1 条 |

诊断脚本及机器可读结果保留在本工作区 `.local/interface-audit-20261107/`，不纳入产品代码。

另外运行现有相关测试，**96 项通过**：

```text
tests/test_chat_compat.py
tests/test_client_identity.py
tests/test_scene_instruction.py
tests/test_scene_instruction_classify.py
tests/test_consultation_sessions.py
tests/test_dify_history.py
tests/test_maxkb_native.py
```

这不等于业务兼容验收通过。场景测试夹具默认使用 `spaceId` / `space_id` 和 `scene_version=7`，成功路径又直接指定工具，未复现附件的“仅小写 spaceid、不传版本、模型选择预览工具”的完整条件。历史列表返回空数组也只能说明 HTTP 请求被受理，不能说明历史记录可见。

`/health/dependencies` 只检查 AI_WebApi 的健康探测地址；T03 的 `reachable` 不包含场景目录四项配置、凭据、点位加载和动作输出验证。

## 5. 交付与修复边界

| 优先级 | 应完成的改动或验证 | 完成判据 |
| --- | --- | --- |
| P0 | 恢复 `spaceid` 兼容及安全的元数据规范化 | 附件原始报文无需改字段即可完成空间检索 |
| P0 | 场景版本改为服务端解析；绑定同次目录证据 | 不传版本可执行，显式过期版本仍被拒绝 |
| P0 | 统一 prompt、路由和工具的执行语义 | 明确导航请求实际产生一次合法动作事件 |
| P0 | 将 AI_WebApi 场景配置纳入部署清单并验证 | 版本/目录查询成功，凭据有效，T09 端到端复测通过 |
| P1 | 旧会话归属校验及兼容映射 | 同归属旧会话可续聊；其他用户和客户端不可读取 |
| P1 | 明确历史列表如何恢复医生角色上下文 | 列表和聊天身份一致；不能用空列表判定通过 |
| P1 | 收口部署补丁与镜像源码 | 记录两个服务的提交、补丁、配置版本及镜像摘要 |

未知项：测试服最新镜像、4 处补丁全文、旧会话仍存活的证据、真实场景成功帧、前端执行回执、目录凭据续期方式。以上不影响已确认的 Git 归因，也不得用猜测填成已验收结果。

## 6. 可复核的提交与代码

- [02ea0ab：集中引入接口变化](https://github.com/Mrpointdelicious/metaAgent/commit/02ea0ab159b9cc394440da76d5f2c249a6bdd1f1)
- [ea9af92：合入 agent，文件树与上项相同](https://github.com/Mrpointdelicious/metaAgent/commit/ea9af92490dabac5a85310a6a111345a4877d851)
- [29f96d4：先前增加 spaceid 等业务兼容](https://github.com/Mrpointdelicious/metaAgent/commit/29f96d4a66d3fcf89be1a90fd9b5705dea4bdf7e)
- [81bdd47：main 患者 v2 身份实现来源](https://github.com/Mrpointdelicious/metaAgent/commit/81bdd47742284fada6e14a76a15481e3f0c60479)
- [2ca551d：MaxKB 非空空间白名单来源](https://github.com/Mrpointdelicious/metaAgent/commit/2ca551d2d6b60885e2b6c1932fbb9daaac5b5e14)
- [832bca2：适老化 prompt 合并](https://github.com/Mrpointdelicious/metaAgent/commit/832bca264bbc753ed119afae7288b255693c7256)
- [空间输入解析](https://github.com/Mrpointdelicious/metaAgent/blob/832bca264bbc753ed119afae7288b255693c7256/src/meta_agent/orchestration/identity.py#L169)、[版本门槛](https://github.com/Mrpointdelicious/metaAgent/blob/832bca264bbc753ed119afae7288b255693c7256/src/meta_agent/domains/scene_instruction.py#L54)、[预览工具实现](https://github.com/Mrpointdelicious/metaAgent/blob/832bca264bbc753ed119afae7288b255693c7256/src/meta_agent/agent/tools.py#L349)、[会话绑定读取](https://github.com/Mrpointdelicious/metaAgent/blob/832bca264bbc753ed119afae7288b255693c7256/src/meta_agent/application/conversations.py#L60)
- [AI_WebApi 场景目录配置及 POST 实现](https://github.com/Mrpointdelicious/metaagent-ai-webapi/blob/df182bfe49459192745c2fa9fb5bb30486e30b8d/AI_WebApi.Services/AITools/Rehab/SceneCommandCatalog.cs#L45)、[可选版本比较及 502 映射](https://github.com/Mrpointdelicious/metaagent-ai-webapi/blob/df182bfe49459192745c2fa9fb5bb30486e30b8d/AI_WebApi/Controllers/AiRehabToolsController.cs#L75)

Git 复核命令（仅在本地仓库读取）：

```powershell
git show --no-patch --format=fuller 02ea0ab
git diff 02ea0ab^1 02ea0ab -- src/meta_agent/orchestration/identity.py
git diff 02ea0ab^1 02ea0ab -- src/meta_agent/domains/scene_instruction.py src/meta_agent/agent/tools.py
git diff 02ea0ab^1 02ea0ab -- src/meta_agent/application/conversations.py
git diff 02ea0ab ea9af92 --stat
git diff 2cfc68f 832bca2 --stat
```
