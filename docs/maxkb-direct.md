# MaxKB 原生 API 直连

`KNOWLEDGE_SOURCES` 可以选择 `protocol=maxkb`，由 Agent 直接访问 MaxKB 原生管理 API。
不启动额外适配服务，不调用 `/v1/retrieve` 或 `/v1/knowledge/search` 桥接端点。
原有 `knowledge-search-v1` 协议仍作为默认值，其他知识源配置保持兼容。

## 配置

在部署 `.env` 中配置以下变量，知识源 JSON 保持单行：

```dotenv
META_AGENT__KNOWLEDGE_SOURCES=[{"id":"maxkb","description":"医院已发布资料","protocol":"maxkb","url":"http://maxkb:8080/admin/api","domains":["health","help","product"],"scoped_domains":["health","help","product"],"maxkb":{"username_env":"MAXKB_NATIVE_USERNAME","password_env":"MAXKB_NATIVE_PASSWORD","catalog_path":"/run/metaagent/maxkb/catalog.json","catalog_sha256":"替换为发布目录的index_sha256","allowed_space_ids":["hospital-a"]}}]
MAXKB_NATIVE_USERNAME=replace-with-existing-account
MAXKB_NATIVE_PASSWORD=replace-with-existing-password
```

凭据可以放在同一 `.env` 中，也支持进程环境变量；进程环境优先。
模型只看到知识源名称、领域和描述，不能指定连接地址、登录凭据、医院空间或知识库 ID。
此方式基于部署中的 MaxKB v2.10.7 管理会话 API；账号需要读取相应知识库及执行命中测试的权限。

`catalog.json` 是现有已发布目录的只读快照，需挂载到配置路径。
使用 JSON 键排序、无多余空格、保留 Unicode 后编码为 UTF-8，计算 SHA-256；
其值应等于既有发布记录中的 `index_sha256`。
目录提供已发布知识库、文档、段落、领域和医院绑定，不能把 UI 中所有资料自动视为已发布。
更新发布目录时应同步更新快照及哈希，并重建新 Agent 容器。

## 调用与边界

Agent 先检查受信任请求中的 `space_id`，再向 MaxKB 原生 `/user/login` 登录。
只对目录内已启用、领域匹配、允许当前医院空间和 operator 读取的知识库调用
`/workspace/{workspace}/knowledge/{knowledge_id}/hit_test`。
返回命中须与已发布原文一致；输出前再次用 MaxKB 原生文档及段落接口确认在线状态和原文。
来源标题、页码、发布版本及段落引用保留在统一工具结果中，上下文按当前预算截断并说明缺口。

不自动回退到桥接、其他知识源或本地语料。不加载 embedding 或 rerank 模型，不创建索引。
原生查询使用 MaxKB 自身的检索配置，不额外调用旧桥接的外部重排流程。

## MaxKB 自身的模型依赖

MaxKB 的原生 `hit_test` 仍会使用该知识库已有的 embedding 模型配置，关键词模式也会先计算向量。
Agent 直连并不会替换 MaxKB 内部的模型供应商或网关设置。
如果现有 MaxKB 模型本身配置了 embedding 网关，它仍有该内部依赖；模型鉴权失败时，
Agent 明确报告知识检索不可用，不把故障当成“没有资料”。
如需整个检索过程均不经过该网关，应由维护方把 MaxKB 的模型配置改为可用的直接供应商连接。
这不要求新增或重建 Agent 的桥接服务。
