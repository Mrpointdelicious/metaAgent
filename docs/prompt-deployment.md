# Prompt 部署与回滚

2026-10-22。版本随应用镜像发布，已打包版本之间通过配置切换。

## 日常操作

在仓库根目录执行；脚本也支持从其他目录以绝对路径启动。

```powershell
# 查询目标镜像实际包含的 bundle、内容 hash 和变更说明。
.\tools\switch-prompt.ps1 -List

# 切换以及回滚，不需要重新构建镜像或发布 Dify。
.\tools\switch-prompt.ps1 -Bundle builtin-v2
.\tools\switch-prompt.ps1 -Bundle builtin-v1
# 2026-10-23 RAG 加固版本（当前部署）。
.\tools\switch-prompt.ps1 -Bundle builtin-v3
```

`builtin-v1` 冻结本次迭代前的线上内容。`builtin-v2` 只更新 `space.context@v2`：
本轮位置优先；换房间不代表历史回答错误；本轮缺少 sceneId 时当前房间未知。
其余 Prompt 复用 v1。默认选择仍为 `builtin-v1`。

`builtin-v3` 使用 `rehab.agent@v2` 和 `space.context@v3`，增加全局医疗文献、
动态已发布场景目录、每轮 4 个子问题及每领域最多 2 次检索约束。它与 RAG 加固代码一起部署；
切换 Prompt 不会修改知识库映射、相关性阈值或 RAG 文献发布。

## 脚本行为

1. 从容器的 Compose 标签发现项目、配置文件及服务最后一个 env_file。
2. 在目标镜像中离线读取、验证 `prompts/releases.json`，未知版本在修改配置前拒绝。
3. 通过仅限容器回环地址且需要服务认证的 `/_ops/prompt-deployment` 暂停新运行。
   已有运行和幂等快照继续处理，同时等待尚在登记的运行完成。默认超时 120 秒，
   可使用 `-TimeoutSeconds 300`；超时恢复接收请求，配置不变。
4. 原子更新 `META_AGENT__PROMPT_BUNDLE`，保留其他配置；若排空期间配置被外部修改则中止。
5. 仅重建 MetaAgent 服务，并核验就绪状态、镜像 ID、服务进程实际绑定的 bundle 与 hash。
   失败时恢复原文件、原镜像和接收请求状态。

单实例切换期间新请求返回可重试的 HTTP 503，当前运行等待结束后再重建。
并发切换通过进程级文件锁互斥。备份和不含凭据的切换结果位于 `.local/prompt-switch/`，
其中 `runtime-before.env` 为私有配置备份；该目录被 Git 忽略。工具不输出凭据。

首次部署此功能需要先发布包含部署控制接口的应用镜像；之后使用上述脚本。
如果 Compose 的 `environment` 显式设置了 `META_AGENT__PROMPT_BUNDLE`，脚本会拒绝修改
较低优先级的 env_file；应先将这一配置移至服务的最后一个 env_file。

## 新增一个版本

1. 新增变化的 `templates/*.vN.md`，在 `catalog.MANIFEST` 注册对应 Prompt 版本。
   不变片段继续复用；输入结构变化时新增对应输入类型。
2. 新增完整的 `BUNDLES` 组合，在 `releases.json` 追加该 bundle 的内容 hash、变更目的和验收记录。
   已发布条目和旧模板均保留；启动和部署预检会核对完整 bundle hash，防止旧版本被原地改写。
3. 运行针对性回归，构建一次包含新旧版本的镜像，并更新部署的镜像配置。
   随后 `-List` 查询到新版本即可切换。

运行记录继续保留 `prompt_bundle`、`prompt_usages`，并增加 `prompt_bundle_hash`。
静态版本元数据不保存患者档案或动态 Prompt 正文；同一 run 的模型循环和重试固定使用创建时绑定。

## 本轮验证

全量 pytest：287 passed、15 skipped（未配置的 PostgreSQL 集成测试和已停用的 MaxKB）。
本轮相关文件 Ruff 检查通过。实际执行 `builtin-v1 → builtin-v2 → builtin-v1`，
两次切换使用同一个镜像，并验证同版本重复执行不重建、未知版本不修改配置。
v2 在现有已发布 Dify 入口完成五项联调：当前位置、同会话换房间、场景缺失、
跨房间医院知识检索及医学文献引用，均成功；最终恢复 `builtin-v1`。
