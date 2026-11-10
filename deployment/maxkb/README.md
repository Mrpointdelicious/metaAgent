# MaxKB 原生工作流部署

这组文件用于配置现有 MaxKB，不启动额外服务，不在运行中的 MaxKB 安装 Python 包或自定义工具。
`build_workflow.py` 仅在部署人员电脑上离线生成工作流 JSON；运行时使用原生节点及 Reply 模板。

实现原理与“介绍二楼”等逐节点示例见 [MaxKB 原生召回实现与过程示例](../../docs/maxkb-native-retrieval-implementation.md)。
从主机准备到模型、知识库、工作流、客户端、备份恢复的完整步骤见 [MaxKB 部署与原生检索接入手册](../../docs/maxkb-deployment-guide.md)。

当前本机基线还有历史 Embedding 适配和启动脚本两个文件挂载；本次召回改进没有新增这些运行代码。
纯官方镜像的模型组合与召回效果尚未验收，不能与当前环境的验证结论混用。

已核对并实测 MaxKB `v2.10.7-lts`，镜像摘要：
`sha256:5ffdafc7956a472ea59ef6820aec5c965282d37c1875109378abae1cbbc2b067`。
其他版本应先检查节点字段及应用 API 兼容性。
[官方工作流文档](https://docs.fit2cloud.com/maxkb/user_manual/app/workflow_app/)

## 文件

| 文件 | 用途 |
| --- | --- |
| [compose.verified.yaml](compose.verified.yaml) | 新环境复现当前方式的单容器模板，需先准备两个历史挂载文件 |
| [compose.official-check.yaml](compose.official-check.yaml) | 纯官方镜像隔离验证模板，使用独立端口和数据卷 |
| [bindings.example.json](bindings.example.json) | 空间、知识库及重排模型绑定模板，ID 均为占位值 |
| [build_workflow.py](build_workflow.py) | 标准库即可运行的一次性配置生成器 |
| [knowledge_reply.jinja](knowledge_reply.jinja) | 原生 Reply 节点的证据 JSON 模板 |
| [native-knowledge-search.workflow.json](native-knowledge-search.workflow.json) | 已生成的可审阅节点图，使用占位绑定 |

本机实测的原生导出文件位于 `.local/maxkb-native/metaagent-native-knowledge-search.mk`，
可从 MaxKB 应用导入入口使用。它绑定本机知识库和模型；移到另一个环境后必须重新绑定。
**工作流 JSON 不是 `.mk` 文件，不能改后缀冒充 UI 导入包。**
`.mk` 不携带本次知识文档、标签及模型凭据，需在目标环境单独准备。

## 1. 在 MaxKB 整理知识库

新建“场景楼层”和“界面操作”两个普通知识库，复用已配置的 embedding 模型。
一份文档只包含一层，段落保留楼层和房间标题；总览独立成文档。把原始多楼层大块移出候选范围，
保留旧库供回滚，不在新工作流中绑定它。

每份文档使用原生标签：

| 标签 | 值 | 要求 |
| --- | --- | --- |
| `space_id` | 实际可信业务空间 ID | 与请求及应用绑定精确一致 |
| `release` | `draft` / `published` | 检索仅接受 `published`；发布前完成索引和原文核对 |
| `floor` | `1` 至 `5` 或 `shared` | 单层文档填写对应数字，概览和操作说明填写 shared |

发布时移除 draft 再添加 published，下线时移除 published 或停用文档。
标签是人工维护规则，不提供跨文档事务或不可变发布版本。不要让一个文档同时保留多个 release 值。
原生检索会排除停用文档/段落。索引失败须先修复，不能靠放宽标签或阈值兜底。

使用原生“关联问题”维护常见别名，例如同一二楼段落关联“介绍二楼”“2楼有什么”“2F有哪些房间”。
禁止把另一楼层的问题关联到该段落。先检查知识库自己的检索测试，再验收完整工作流；
仅调用 `hit_test` 不会执行下述文档筛选和重排流程。

## 2. 导入或生成工作流

优先导入本机导出的 `.mk`，在画布检查和修改以下节点，再发布新应用：

| 节点 | 需检查的配置 |
| --- | --- |
| `base-node` | API 输入 domain、space_id、max_context_tokens、request_id |
| `scope-guard` | space_id 等于目标空间；domain 仅 help/product |
| `scene-published` / `help-published` | 批准的知识库 ID；`space_id AND release=published` |
| `floor-1` 至 `floor-5` | 正则别名；其他医院的楼层范围需相应调整 |
| `floor-documents` | 引用 scene-published 的文档；目标 floor 标签 OR；不能改为全库范围 |
| `search` | 引用 global.document_scope；blend、12 条、阈值 0.25 |
| `Evidence` | 已配置重排模型；4 条、阈值 0.1 |
| `reply` | 保留原文 JSON 模板，不能替换为普通回答模型 |

若使用 JSON 配置流程，复制 bindings 模板至私有目录，填入真实空间和原生对象 ID：

```powershell
python deployment/maxkb/build_workflow.py --bindings .local/maxkb-native/bindings.json --output .local/maxkb-native/workflow.json
```

`reranker_model_id` 可设为 null 生成无重排版本；其效果未包含在当前实测结论中。
模板默认只处理一至五楼；新空间的楼层集合应同步修改生成器的 `FLOORS` 及对应原生条件，
不要只换知识库而保留错误楼层标签。

生成器不连接服务器，不读取管理密码，不自动修改已有应用。
将 JSON 作为 `work_flow` 字段通过 MaxKB 管理 API 创建**新工作流应用**，或更新专门创建的验证应用：

```text
POST /admin/api/workspace/{workspace_id}/application
body: {name, desc, prologue:"", type:"WORK_FLOW", folder_id, work_flow:<生成的 JSON>}

PUT /admin/api/workspace/{workspace_id}/application/{application_id}
body: {work_flow:<生成的 JSON>}

PUT /admin/api/workspace/{workspace_id}/application/{application_id}/publish
body: {}

GET /admin/api/workspace/{workspace_id}/application/{application_id}/export
response: 原生 .mk 文件
```

这些是一次性部署操作，使用部署人员的原生管理鉴权；管理凭据不进入 MetaAgent。
当前版本部分失败通过 HTTP 200 的 `code` 返回，部署工具须同时检查业务 code 和 HTTP 状态。
导出以真实安装版本为准：v2.10.7-lts 实际导出方法为 GET，不能仅根据注释推断为 POST。

## 3. 发布与接入

在 MaxKB 检查节点连线、知识库和模型权限，然后发布应用，创建专用应用 API Key。
MetaAgent 配置 `transport=maxkb-workflow` 和该应用的 `/chat/api/{id}/chat/completions`。
[完整配置与探测命令](../../docs/maxkb-knowledge-search-v1.md)

每个应用固定一个空间。多个空间分别维护应用或在 MaxKB 内建立经验证的原生路由，
本模板不允许客户端动态传知识库 ID。API Key 是服务凭据，不能交给终端用户任意请求其他空间。
应用日志会记录检索问题及证据，按现有 MaxKB 日志管理方式设置保留范围。

至少检查：二楼中文/数字/2F 别名、三楼对照、二三楼比较、help 域楼层问题、界面操作、
不存在楼层、没有位置的“这里”、空间不匹配、未发布/停用资料、预算不足、模型服务故障。
需要确认来源 ID 与原文，不能只看接口 200 或最终答案是否流畅。

## 本机已落地状态

- 仅启动现有 `metarag-maxkb-maxkb-1`，管理页 `http://127.0.0.1:18081/admin/`。
- 创建隔离的两个验证知识库、13 份原文文档，以及应用 `MetaAgent原生知识检索验证20261110`。
- 应用 ID `01a1c376-418c-7e81-a87c-c0c3bbd4e36d`，已发布，复用已有 embedding/rerank 模型。
- 连接凭据留在 `.local/maxkb-native/connection.env`；状态和绑定留在同目录，均不入 Git。
- 保留旧库、旧应用和业务配置；未启动旧 `/v1/retrieve` 桥接服务，未切换测试服。

本机旧资料以医院中文名为测试空间，并非业务 UUID；目标环境必须重新核对绑定。
实际样本与范围见 [验证记录](../../docs/maxkb-native-verification-20261110.md)。
