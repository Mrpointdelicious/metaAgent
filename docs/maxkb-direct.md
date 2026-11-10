# MaxKB 原生直连迁移说明

自 2026 年 11 月 10 日起，MetaAgent 统一使用 `knowledge-search-v1` 证据契约。
旧 `protocol=maxkb` 配置不再被接受，管理账号登录、发布目录生成与挂载、逐库 `hit_test`、
候选排序、原文二次校验和上下文组装均退出 MetaAgent。

现推荐 `transport=maxkb-workflow`：调用 MaxKB 原生应用的 `/chat/api/{application_id}/chat/completions`，
将请求包成 `messages + form_data`，读取原生 Reply 模板返回的 v1 JSON。
这是薄协议适配，不恢复管理 API 逐库召回，也不要求额外的 `/v1/knowledge/search` 服务。
MaxKB 的原生文档筛选、知识库检索和重排节点执行召回策略。

部署迁移步骤见 [MaxKB 接入说明](maxkb-knowledge-search-v1.md)，
服务端职责与召回改进见 [MaxKB 召回方案](maxkb-retrieval-improvement-plan.md)。
原生直连实现和旧部署说明可在 Git 历史中查阅；已有私有目录文件不会被删除。
