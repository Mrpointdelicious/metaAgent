# MaxKB 部署与原生检索接入手册

更新日期：2026-11-10。适用版本：MaxKB `v2.10.7-lts`。
目标是部署 MaxKB、发布原生检索工作流，并让 MetaAgent 通过原生应用 API 获取 `knowledge-search-v1` 证据。
最终链路为：MetaAgent 薄客户端 → MaxKB 应用 API → 文档筛选 → 混合检索 → 重排 → Reply。
不部署旧 bridge、Portfolio 或新的检索网关。

## 1 先确认部署方式

| 方式 | 使用场景 | 验证范围 |
| --- | --- | --- |
| 复用现有本机部署 | 使用当前已运行的 MaxKB，保留已有资料和模型 | 当前实际环境，已完成召回验证 |
| 在新环境复现当前方式 | 固定官方镜像，保留两个已有文件挂载 | 原方式已验证；新环境仍需验收 |
| 纯官方镜像独立验证 | 希望逐步取消历史文件挂载 | 提供配置，但尚未验证当前模型组合和召回结果 |

**当前环境包含两个历史定制文件，不能把本地验证结论写成“未经定制的官方镜像已通过验证”。**
本次召回改进只新增原生工作流配置；下面两个挂载此前已存在：

| 文件 | 作用 | 是否新增服务 |
| --- | --- | --- |
| `provider_embedding.py` | 在百炼模型适配中区分 query 与 document 的 Embedding 参数 | 否，覆盖容器内一个模型适配文件 |
| `start-maxkb.sh` | 使用单个 Gunicorn worker 和 Celery 线程任务，跳过本地模型服务启动 | 否，替换容器内启动脚本 |

本文的 Compose 配置是单容器安装方式，PostgreSQL、Redis 和 MaxKB 在同一容器中运行。
官方另外推荐生产环境使用离线安装包；离线安装包的 `mkctl` 命令和拓扑不能直接套用到本文 Compose 环境。
[官方离线安装说明](https://docs.fit2cloud.com/maxkb/installation/offline_installtion/)

### 当前本机基线

| 项目 | 当前值 |
| --- | --- |
| 原部署目录 | `D:\Project\MetaRag` |
| 原部署文件 | [compose.maxkb.yaml](D:/Project/MetaRag/compose.maxkb.yaml) |
| 容器 | `metarag-maxkb-maxkb-1` |
| 管理入口 | [本机 MaxKB](http://127.0.0.1:18081/admin/) |
| 数据卷 | `metarag-maxkb_maxkb_data`，挂载到 `/opt/maxkb` |
| 资源限制 | 2 CPU、4 GB，仅代表本地验证配置 |
| 已发布应用 | `MetaAgent原生知识检索验证20261110` |
| 应用 ID | `01a1c376-418c-7e81-a87c-c0c3bbd4e36d` |

本机现有 Compose 还定义了旧 bridge、旧验证 MetaAgent 和 OCR。
复用现有环境时务必在命令末尾指定 `maxkb`，不要直接启动整套旧服务。
当前数据卷已初始化，应使用现有管理员账号；初始默认密码不代表当前账号密码。

## 2 准备主机与部署文件

### 2.1 主机和网络

服务器规划采用至少 4 核、8 GB 内存、100 GB 磁盘作为官方基础参考，实际容量取决于资料量和并发。
Docker 使用支持 Compose 插件的版本；官方建议 Docker 26 或更高。
Windows 使用 Docker Desktop 的 Linux 容器模式；Linux 可采用 Ubuntu 22.04。
[官方部署要求](https://docs.fit2cloud.com/maxkb/installation/online_installtion/)

如果主机尚未安装 Docker：

- Windows：按 [Docker Desktop 安装说明](https://docs.docker.com/desktop/setup/install/windows-install/) 启用相应虚拟化后端并安装，启动 Docker Desktop，使用 Linux 容器。
- Ubuntu：按 [Docker Engine 安装说明](https://docs.docker.com/engine/install/ubuntu/) 配置官方软件源，安装 Engine、CLI、containerd、Buildx 和 Compose 插件，启动 Docker 服务。

已有 Docker 且检查正常时直接复用，不为部署 MaxKB 卸载现有容器环境。

先执行：

```text
docker version
docker compose version
docker info --format "{{.OSType}}/{{.Architecture}}"
```

预期 Docker 客户端和服务端均可用，OSType 为 linux。模型服务通过网络调用时，还需允许容器访问模型供应商地址。
本机核查版本为 Docker 29.7.2、Compose v5.5.1；这不是强制要求必须升级到同一版本。

### 2.2 固定镜像

本文固定使用以下完整镜像引用，不使用浮动的 latest：

```text
registry.fit2cloud.com/maxkb/maxkb:v2.10.7-lts@sha256:5ffdafc7956a472ea59ef6820aec5c965282d37c1875109378abae1cbbc2b067
```

该摘要已用于当前本机验证。新机器需确认镜像与 CPU 架构匹配；架构不同或升级版本时重新核对摘要与兼容性。

### 2.3 新环境准备目录

新部署使用单独目录，示例为 Windows `D:\Deploy\maxkb-native` 或 Linux `/opt/maxkb-native`。
目录结构如下：

```text
maxkb-native/
  compose.verified.yaml
  deployment.env
  runtime/
    provider_embedding.py
    start-maxkb.sh
  workflow/
    build_workflow.py
    bindings.example.json
    knowledge_reply.jinja
    bindings.json
  backups/
```

Windows 在本机复制已存在的文件，示例中 $metaAgentRepo 应指向本次修改所在的仓库：

```powershell
$metaAgentRepo = 'C:\Users\weidaohao\.codex\worktrees\knowledge-search-v1\metaAgent'
$maxkbDeploy = 'D:\Deploy\maxkb-native'
New-Item -ItemType Directory -Path "$maxkbDeploy\runtime","$maxkbDeploy\workflow","$maxkbDeploy\backups" -Force | Out-Null
Copy-Item -LiteralPath "$metaAgentRepo\deployment\maxkb\compose.verified.yaml" -Destination "$maxkbDeploy\compose.verified.yaml"
Copy-Item -LiteralPath 'D:\Project\MetaRag\maxkb\provider_embedding.py' -Destination "$maxkbDeploy\runtime\provider_embedding.py"
Copy-Item -LiteralPath 'D:\Project\MetaRag\maxkb\start-maxkb.sh' -Destination "$maxkbDeploy\runtime\start-maxkb.sh"
Copy-Item -LiteralPath "$metaAgentRepo\deployment\maxkb\build_workflow.py","$metaAgentRepo\deployment\maxkb\bindings.example.json","$metaAgentRepo\deployment\maxkb\knowledge_reply.jinja" -Destination "$maxkbDeploy\workflow"
```

新 Linux 服务器接收同样的目录和文件，确保启动脚本为 LF 换行且可执行：

```bash
cd /opt/maxkb-native
chmod 755 runtime/start-maxkb.sh
```

文件从本机准备后传到目标服务器，不要复制当前连接凭据作为新环境凭据。
两个 runtime 文件是复现当前环境的前置材料；缺失时先准备文件，不要让 Docker 自动创建同名目录。
新 Compose 使用 `create_host_path: false`，用于尽早发现此类路径错误。

### 2.4 新环境配置文件

创建 `deployment.env`，内容如下。这些变量只用于容器部署，不是 MetaAgent 的知识源配置：

```dotenv
MAXKB_BIND_IP=127.0.0.1
MAXKB_PORT=18081
MAXKB_DATA_VOLUME=maxkb_native_data
MAXKB_MEMORY=4g
MAXKB_CPUS=2
MAXKB_EMBEDDING_FILE=./runtime/provider_embedding.py
MAXKB_START_FILE=./runtime/start-maxkb.sh
```

这份配置复现本地容器的资源限制；扩大资料量或并发时按监控调整。
若与当前本机容器同时运行，把端口改成空闲的 `18092`，数据卷继续使用独立的新卷。
不要让两个容器同时挂载同一份 PostgreSQL 数据。

新模板的默认卷 `maxkb_native_data` 与旧卷 `metarag-maxkb_maxkb_data` 不同。
新模板首次启动得到空白实例是预期行为；需要旧数据时执行第 10 节的恢复流程。

## 3 启动并检查 MaxKB

### 3.1 复用现有本机环境

仅在已有 `D:\Project\MetaRag` 环境使用本节，使用后可直接进入模型与知识库检查：

```powershell
Set-Location 'D:\Project\MetaRag'
docker compose -f compose.maxkb.yaml config --quiet
docker compose -f compose.maxkb.yaml up -d maxkb
docker compose -f compose.maxkb.yaml ps maxkb
docker compose -f compose.maxkb.yaml logs --tail 80 maxkb
```

本次只编写和核验步骤，没有执行重建。编写时容器正常运行，但实际 RestartPolicy 为 `no`，
与原 Compose 的 `unless-stopped` 不一致。若要纠正现有容器而不重建，可执行：

```powershell
docker update --restart unless-stopped metarag-maxkb-maxkb-1
docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' metarag-maxkb-maxkb-1
```

此命令是待部署人员执行的修正步骤，尚未替用户执行；修改后预期输出 `unless-stopped`。

### 3.2 启动新部署

在第 2 节准备好的部署目录执行，Windows 用 `Set-Location`，Linux 用 `cd`。
以下 Docker 命令两种终端相同：

```text
docker compose --env-file deployment.env -f compose.verified.yaml config --quiet
docker compose --env-file deployment.env -f compose.verified.yaml pull maxkb
docker compose --env-file deployment.env -f compose.verified.yaml up -d maxkb
docker compose --env-file deployment.env -f compose.verified.yaml ps maxkb
docker compose --env-file deployment.env -f compose.verified.yaml logs --tail 80 maxkb
```

首次启动需等待初始化和数据库迁移完成。预期服务状态为 running/healthy，日志没有持续的数据库、文件权限或模型启动错误。
`config --quiet` 仅验证配置，不证明启动成功。

PowerShell 进一步核对实际挂载和重启策略：

```powershell
$maxkbContainerId = docker compose --env-file deployment.env -f compose.verified.yaml ps -q maxkb
docker inspect --format '{{.State.Health.Status}}' $maxkbContainerId
docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' $maxkbContainerId
docker inspect --format '{{range .Mounts}}{{println .Type .Name .Destination}}{{end}}' $maxkbContainerId
(Invoke-WebRequest -Uri 'http://127.0.0.1:18081/admin/' -UseBasicParsing -TimeoutSec 10).StatusCode
```

如果修改过端口，同步修改探测 URL。预期挂载包括 `/opt/maxkb`、Embedding 文件和启动脚本，管理页 HTTP 为 200。
本镜像的 v2 数据位于 `/opt/maxkb`；不要把额外出现的 `/var/lib/postgresql/data` 匿名卷误当成主备份目标。

### 3.3 登录与凭据

首次新建空数据卷后，打开 `http://部署主机:映射端口/admin/`。
官方默认账号为 `admin`，默认密码为 `MaxKB@123..`，首次登录后修改。
已有数据卷保留原账号和密码，不会因重建容器恢复默认值。
[官方首次登录说明](https://docs.fit2cloud.com/maxkb/installation/online_installtion/)

管理账号用于维护；模型 API Key 填入 MaxKB 模型配置；MetaAgent 使用第 6 节生成的应用 API Key。
三类凭据分别使用，不把管理员密码填进 MetaAgent 知识源。

## 4 配置 Embedding 与重排模型

在 MaxKB 的模型管理中完成下列操作：

1. 添加 Embedding 模型，填写目标环境的供应商、Base URL、实际模型名称和凭据。
2. 执行模型连接测试，确认可以成功生成向量。
3. 添加 Reranker 模型，填写相应连接参数并测试。
4. 确认工作空间和部署账号有权使用两个模型，记录模型 ID。

当前实例的已配置名称如下，供对照，不能保证其他账号或模型网关支持同名模型：

| 类型 | 当前模型名称 | 使用位置 |
| --- | --- | --- |
| Embedding | `qwen3.7-text-embedding` | 场景库与操作库的向量化和查询 |
| Reranker | `qwen3.7-text-rerank` | 原生工作流 Evidence 节点 |

本流程不使用回答 LLM，Reply 直接格式化原文，因此不需要为检索应用额外配置对话模型。
复现当前方式时，启动脚本不启动本地模型服务，应绑定实际可用的外部模型。
更换 Embedding 模型后重新完成相关资料向量化，再验证检索，不能直接沿用旧向量宣称兼容。

## 5 创建知识库和发布资料

### 5.1 创建两个普通知识库

创建“医院场景楼层”和“界面操作说明”，分别选择已验证的 Embedding 模型。
记录两个知识库 ID。场景库按楼层拆成文档，操作库放按钮、麦克风、返回等说明。

| 文档例子 | space_id | floor | release |
| --- | --- | --- | --- |
| 二楼问诊中心 | 真实业务空间 ID | `2` | `draft` → `published` |
| 三楼诊疗中心 | 同一空间 ID | `3` | `draft` → `published` |
| 医院总览 | 同一空间 ID | `shared` | `draft` → `published` |
| 麦克风操作 | 同一空间 ID | `shared` | `draft` → `published` |

标签在原生文档标签中维护。一个文档不混放多个楼层，段落保留楼层与房间标题及完整原文。
为楼层段落关联“介绍二楼”“2楼有什么”“2F有哪些房间”等问题。

### 5.2 发布顺序

1. 以 draft 标签导入，核对空间、楼层、标题与原文。
2. 等待文档和关联问题索引完成，处理所有索引失败。
3. 在知识库检索测试中确认目标段落能命中。
4. 移除 draft，添加 `release=published`，保持文档和段落启用。
5. 后续下线时移除 published 或停用文档。

单独的知识库 `hit_test` 不执行工作流中的标签筛选；第 8 节必须通过应用 API 再验收一次。
资料拆分与发布过程示例见 [原生召回实现说明](maxkb-native-retrieval-implementation.md)。

## 6 导入并发布原生检索工作流

### 6.1 准备实际绑定

复制 `workflow/bindings.example.json` 为 `workflow/bindings.json`，填入目标环境的真实值：

```json
{
  "name": "医院原生知识检索",
  "space_id": "replace-with-real-business-space-id",
  "scene_knowledge_ids": ["00000000-0000-4000-8000-000000000001"],
  "help_knowledge_ids": ["00000000-0000-4000-8000-000000000002"],
  "reranker_model_id": "00000000-0000-4000-8000-000000000003",
  "similarity": 0.25,
  "rerank_similarity": 0.1
}
```

上面的 UUID 均为占位符。空间值必须与业务请求和文档标签精确一致。
本地验证资料沿用医院中文名作为测试空间，不能把它直接当作正式业务 UUID。
当前工作流只配置一至五楼；其他楼层还需同步更新原生条件和标签。

### 6.2 方式一 导入原生 mk 文件

已有本机导出文件为仓库下的 `.local/maxkb-native/metaagent-native-knowledge-search.mk`。
通过 MaxKB 应用管理的导入入口导入到目标工作空间，再逐项修改：

| 节点 | 目标环境要修改或核对的值 |
| --- | --- |
| `scope-guard` | 实际可信 space_id；允许 help/product |
| `scene-published` | 新场景库 ID、实际 space_id、release=published |
| `help-published` | 新操作库 ID、实际 space_id、release=published |
| `floor-1` 至 `floor-5` | 本院楼层及别名范围 |
| `floor-documents` | 只能引用已发布场景文档，按 floor 作 OR 筛选 |
| `search` | 引用 global.document_scope，blend、12 条、阈值 0.25 |
| `Evidence` | 新环境重排模型 ID，4 条、阈值 0.1 |
| `reply` | 保留原文证据 JSON 模板 |

`.mk` 不包含本次知识资料、标签和模型凭据，目标环境须先完成第 4、5 节。
导入后先保存并检查资源引用；工作流 JSON 不能改后缀冒充 `.mk`。

### 6.3 方式二 离线生成配置后调用管理 API

不使用 `.mk` 时，可在有 Python 3 的部署电脑上生成配置；生成器只依赖标准库：

```text
python workflow/build_workflow.py --bindings workflow/bindings.json --output workflow/workflow.json
```

当前含重排的模板生成 28 个原生节点、34 条连线。生成器不连接服务器，不作为常驻程序运行。
下面 PowerShell 示例创建新的应用，不覆盖旧应用；在部署目录执行，地址按实际端口修改。

```powershell
$maxkbAdmin = 'http://127.0.0.1:18081/admin/api'
$maxkbWorkspace = 'default'
$maxkbHeaders = @{}
function Invoke-MaxkbAdmin {
    param([string]$Method, [string]$Path, $Body)
    $json = $Body | ConvertTo-Json -Depth 100 -Compress
    $result = Invoke-RestMethod -Method $Method -Uri "$maxkbAdmin$Path" -Headers $maxkbHeaders -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($json))
    if ($result.code -notin @(0, 200)) { throw 'MaxKB 管理操作失败，请在服务端查看原因' }
    return $result.data
}
$maxkbUser = Read-Host 'MaxKB 管理员用户名'
$maxkbPassword = Read-Host 'MaxKB 管理员密码' -AsSecureString
$loginBody = @{ username=$maxkbUser; password=([Net.NetworkCredential]::new('', $maxkbPassword)).Password }
$login = Invoke-MaxkbAdmin 'POST' '/user/login' $loginBody
$maxkbHeaders = @{ Authorization='Bearer ' + $login.token }
$loginBody.password = $null
$flow = Get-Content -LiteralPath './workflow/workflow.json' -Raw -Encoding UTF8 | ConvertFrom-Json
$bindings = Get-Content -LiteralPath './workflow/bindings.json' -Raw -Encoding UTF8 | ConvertFrom-Json
$createBody = @{ name=$bindings.name; desc='原生节点检索'; prologue=''; type='WORK_FLOW'; folder_id='default'; work_flow=$flow }
$app = Invoke-MaxkbAdmin 'POST' "/workspace/$maxkbWorkspace/application" $createBody
$maxkbAppId = $app.id
$maxkbAppId
```

记录返回的新应用 ID，在 MaxKB 画布检查配置后发布。重复执行创建语句会新增应用，应保留返回 ID。
若账号使用其他工作空间或文件夹，替换示例中的 default。

发布可通过 UI，或在同一 PowerShell 会话执行：

```powershell
$null = Invoke-MaxkbAdmin 'PUT' "/workspace/$maxkbWorkspace/application/$maxkbAppId/publish" @{}
```

本版本部分 API 失败仍使用 HTTP 200，必须像示例一样检查业务 code。

### 6.4 创建应用凭据并记录发布结果

在该应用的 API 访问设置中创建专用应用 API Key，保存到部署机私有配置。
记录应用 ID、发布版本、绑定知识库及模型 ID，以及实际 space_id。
对外检索使用应用 Key；上面的管理登录 token 仅用于部署操作。

发布后可以导出原生 `.mk`，当前版本实际接口方法为 GET：

```text
GET /admin/api/workspace/{workspace_id}/application/{application_id}/export
```

## 7 配置 MetaAgent 与网络

### 7.1 选择实际可达的原生应用地址

| MetaAgent 所在位置 | URL 选择 |
| --- | --- |
| 同机宿主机进程 | `http://127.0.0.1:18081/chat/api/APP_ID/chat/completions` |
| 与 MaxKB 共享 Docker 网络 | `http://maxkb-native:8080/chat/api/APP_ID/chat/completions`，需配置下述网络别名 |
| 另一台机器 | 已配置的内网地址或 HTTPS 入口，路径仍为 `/chat/api/APP_ID/chat/completions` |

容器中的 127.0.0.1 指向容器自身。宿主机端口若只绑定 127.0.0.1，也不能假定其他主机可访问。
跨主机部署按实际内网 IP 设置 MAXKB_BIND_IP，或接入已有 HTTPS 代理；应用 API Key 随 HTTP 请求发送，
跨不可信网络使用 HTTPS。单容器安装无需向调用方开放 PostgreSQL 和 Redis 端口。

### 7.2 两个 Compose 项目共享网络

MetaAgent 在 Docker 中且与 MaxKB 同机时，创建一次外部网络：

```text
docker network create metaagent-knowledge
```

若网络已存在，不重复创建。MaxKB 部署目录新增 `compose.network.yaml`：

```yaml
services:
  maxkb:
    networks:
      default: {}
      knowledge:
        aliases: [maxkb-native]
networks:
  knowledge:
    external: true
    name: metaagent-knowledge
```

以后 MaxKB 的部署命令同时携带两个 Compose 文件：

```text
docker compose --env-file deployment.env -f compose.verified.yaml -f compose.network.yaml up -d maxkb
```

MetaAgent 自己的 Compose 目录新增 `compose.knowledge-network.yaml`，保留原数据库网络：

```yaml
services:
  meta_agent:
    networks:
      default: {}
      knowledge: {}
networks:
  knowledge:
    external: true
    name: metaagent-knowledge
```

这只建立容器网络连接，不启动额外网关。不同环境使用不同网络或唯一服务别名，避免别名冲突。
后续运维命令应始终带相同的 Compose 文件组合。

### 7.3 知识源配置

在 MetaAgent 实际使用的私有 `.env` 中加入以下配置，JSON 必须单行：

```dotenv
META_AGENT__KNOWLEDGE_SOURCES=[{"id":"maxkb","description":"已发布的医院楼层场景与界面操作说明","protocol":"knowledge-search-v1","transport":"maxkb-workflow","url":"http://maxkb-native:8080/chat/api/REPLACE_APP_ID/chat/completions","domains":["help","product"],"scoped_domains":["help","product"],"token_env":"MAXKB_KNOWLEDGE_TOKEN","timeout_seconds":20}]
META_AGENT__KNOWLEDGE_CONTEXT_TOKENS=4000
MAXKB_KNOWLEDGE_TOKEN=replace-with-application-api-key
```

根据第 7.1 节替换 URL，再填写实际应用 ID 与 Key。
MetaAgent 只转换请求包装和响应格式；MaxKB 没有因此新增 `/v1/knowledge/search` 路由。
该工作流覆盖 help/product，不能直接声明已经覆盖 health、设备资料或其他医院。

移除旧 protocol=maxkb、管理 API 连接、MAXKB_NATIVE_*、PORTFOLIO_* 等旧链路配置。
默认 KNOWLEDGE_SOURCES=[] 表示尚未启用。进程环境中的变量优先于 dotenv，排查时注意旧环境变量覆盖。
容器通过 env_file 注入 Key；不能只把带密钥文件放在宿主机而不让运行进程读取。

## 8 探测 接入与验收

### 8.1 先检查客户端配置

在本次分支的 MetaAgent 仓库、已安装依赖的 Python 环境中执行。
尚未安装依赖时先按项目流程安装；`uv sync --locked` 可安装核心依赖，业务部署额外依赖按现有配置保留。
不要在旧镜像中寻找尚未构建进去的 transport=maxkb-workflow 实现。

切换业务前，在仓库的私有目录创建 `.local/maxkb-probe.env`，内容沿用第 7.3 节，
但 URL 改为部署电脑可访问的宿主机映射地址或 HTTPS 入口。
例如本机用 127.0.0.1:18081；Docker 内部别名只用于后续业务容器配置。

```text
uv run python -m meta_agent.integrations.knowledge_probe --env-file .local/maxkb-probe.env --source maxkb --domain product --space-id REPLACE_SPACE_ID --query "介绍二楼" --check-config
```

预期退出码 0，check=configuration、network_verified=false。这一步完全不发网络请求。

去掉 --check-config，执行一次真实调用：

```text
uv run python -m meta_agent.integrations.knowledge_probe --env-file .local/maxkb-probe.env --source maxkb --domain product --space-id REPLACE_SPACE_ID --query "介绍二楼"
```

如果 URL 使用 Docker 内部别名，应在已连接共享网络的 MetaAgent 容器内运行探测，而不是宿主机。
容器命令为 `python -m meta_agent.integrations.knowledge_probe`，参数相同；使用注入的环境变量时省略 --env-file。

### 8.2 检查原生 API 的实际证据

仅接口成功不足以证明召回正确。可在部署电脑用应用 Key 调用原生 API，查看来源和正文：

```powershell
$maxkbBase = 'http://127.0.0.1:18081'
$maxkbAppId = 'REPLACE_APP_ID'
$maxkbSpace = 'REPLACE_SPACE_ID'
$maxkbKeySecure = Read-Host 'MaxKB 应用 API Key' -AsSecureString
$maxkbKey = ([Net.NetworkCredential]::new('', $maxkbKeySecure)).Password
$body = @{
    messages=@(@{role='user';content='介绍二楼'})
    stream=$false
    form_data=@{domain='product';space_id=$maxkbSpace;max_context_tokens=4000;request_id='deployment-smoke-1'}
} | ConvertTo-Json -Depth 8 -Compress
$wire = Invoke-RestMethod -Method Post -Uri "$maxkbBase/chat/api/$maxkbAppId/chat/completions" -Headers @{Authorization='Bearer ' + $maxkbKey} -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
if ($wire.choices.Count -ne 1 -or $wire.choices[0].finish_reason -ne 'stop') { throw '原生工作流响应异常' }
$evidence = $wire.choices[0].message.content | ConvertFrom-Json
$evidence | Select-Object status,trace_id,gaps
$evidence.sources | Select-Object id,title,locator,ref
$evidence.context
$maxkbKey = $null
```

此诊断展示检索正文，只使用非敏感验收题。实际 MetaAgent 客户端还会执行 v1 Schema 校验、超时和体积限制。
预期二楼题仅包含二楼来源，trace_id 可对应 MaxKB 对话日志中的节点过程。

### 8.3 必测用例

| 问题或操作 | 预期 |
| --- | --- |
| 介绍二楼、介绍2楼、二层有什么、2F有哪些房间 | 仅目标楼层来源 |
| 介绍三楼 | 与二楼保持区分 |
| 二楼和三楼有什么区别 | 当前样本应包含两层，人工核对内容覆盖 |
| 介绍六楼、介绍这里 | 当前模板返回 no_evidence 和明确缺口 |
| help 域问二楼 | 进入场景分支 |
| product 域问麦克风 | 进入操作库，含对应原文 |
| 换成未授权 space_id | 权限错误，不返回其他空间证据 |
| 验证文档移除 published 或停用 | 不再返回该文档，不跨层兜底；验证后恢复 |
| max_context_tokens 改成 600 或 128 | 截断时 partial，容纳不下时 no_evidence，均带预算缺口 |
| 模型服务异常 | 明确失败，不当作无资料；只在隔离环境验证 |

本机既有结果为 20 个固定样本通过，12 个单层问题均未出现异楼层来源；不是目标环境自动验收结果。
业务还需补充不参与调参的保留题，检查错误率、无证据比例和延迟。

### 8.4 最后切换业务进程

在 MetaAgent 目录，先构建包含本次薄适配代码的镜像，再按原有部署流程加载配置。
若采用第 7.2 节共享网络，示例命令为：

```text
docker compose -f compose.yaml -f compose.knowledge-network.yaml config --quiet
docker compose -f compose.yaml -f compose.knowledge-network.yaml build meta_agent
docker compose -f compose.yaml -f compose.knowledge-network.yaml up -d --no-deps --force-recreate meta_agent
```

前置条件是 MetaAgent 自身数据库及其他依赖已经按原业务方式启动。
不要用 restart 代替重新注入变更后的 env_file；切换后在容器内重跑探测，并验证一轮真实业务问答。
本次文档工作没有执行业务切换。

## 9 纯官方镜像的独立验证步骤

如果要取消两个历史挂载，使用 [compose.official-check.yaml](../deployment/maxkb/compose.official-check.yaml)
创建独立验证实例。它没有文件覆盖，也没有关闭模型下载的 HF/Transformers 离线标记。
默认端口 18091、数据卷 maxkb_official_check_data，与已验证环境分开。

在独立目录只放入该文件，创建自己的 official.env：

```dotenv
MAXKB_BIND_IP=127.0.0.1
MAXKB_PORT=18091
MAXKB_DATA_VOLUME=maxkb_official_check_data
MAXKB_MEMORY=8g
MAXKB_CPUS=4
```

```text
docker compose --env-file official.env -f compose.official-check.yaml config --quiet
docker compose --env-file official.env -f compose.official-check.yaml pull maxkb
docker compose --env-file official.env -f compose.official-check.yaml up -d maxkb
docker compose --env-file official.env -f compose.official-check.yaml ps maxkb
```

随后重新完成模型、少量样本文档、标签、工作流和第 8 节验收。
重点验证 Embedding 查询/文档接口与当前供应商兼容、索引成功、重排可用，以及启动资源占用。
验证通过才考虑在目标环境取消旧挂载；不能直接在现有数据卷上去掉挂载就认定等价。

当前仅验证了这份新模板的 Compose 语法，没有启动或验收纯官方实例。
官方镜像基础安装方式参见 [在线安装](https://docs.fit2cloud.com/maxkb/installation/online_installtion/)。

## 10 数据备份 恢复与升级

### 10.1 要备份的内容

完整备份至少包括 `/opt/maxkb` 数据卷、Compose 和部署变量、两个历史挂载文件、资料原件以及工作流绑定。
单独导出的 `.mk` 不能替代数据库备份。应用和模型凭据、启动密钥等可能保存在备份中，备份按私有部署材料保管。
官方同版本恢复说明也要求保存完整运行目录。
[官方备份说明](https://docs.fit2cloud.com/maxkb/installation/backup/)

### 10.2 冷备示例

下面步骤会短暂停止 MaxKB，应在维护窗口执行；文档编写期间没有执行。
示例针对新部署的单份 compose.verified.yaml；若使用额外网络文件，在相关 Compose 命令中带齐文件。
先核对容器、主卷和备份路径，避免备份到一个新建的空卷：

```powershell
Set-Location 'D:\Deploy\maxkb-native'
$maxkbContainerId = docker compose --env-file deployment.env -f compose.verified.yaml ps -q maxkb
if (-not $maxkbContainerId) { throw '未找到运行中的 MaxKB，请核对部署目录' }
$maxkbInfo = (docker inspect $maxkbContainerId | ConvertFrom-Json)[0]
$maxkbMount = @($maxkbInfo.Mounts | Where-Object { $_.Destination -eq '/opt/maxkb' })
if ($maxkbMount.Count -ne 1 -or $maxkbMount[0].Type -ne 'volume') { throw '请按实际挂载类型调整备份方式' }
$maxkbVolume = $maxkbMount[0].Name
$maxkbImage = $maxkbInfo.Config.Image
$maxkbBackupDir = Join-Path (Get-Location).Path ('backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $maxkbBackupDir | Out-Null
$maxkbVolume
$maxkbBackupDir
```

核对输出后，停止容器，用同一镜像的 tar 命令作一次性备份；不会启动另一个 MaxKB 服务：

```powershell
docker compose --env-file deployment.env -f compose.verified.yaml stop -t 120 maxkb
if ($LASTEXITCODE -ne 0) { throw '停止失败，不继续复制数据库文件' }
try {
    docker run --rm --network none --read-only --entrypoint tar --mount "type=volume,source=$maxkbVolume,target=/source,readonly" --mount "type=bind,source=$maxkbBackupDir,target=/backup" $maxkbImage -C /source -czf /backup/maxkb-data.tar.gz .
    if ($LASTEXITCODE -ne 0) { throw '数据卷备份失败' }
    Copy-Item -LiteralPath './compose.verified.yaml','./deployment.env' -Destination $maxkbBackupDir
    Copy-Item -LiteralPath './runtime','./workflow' -Destination $maxkbBackupDir -Recurse
    Get-FileHash -LiteralPath "$maxkbBackupDir\maxkb-data.tar.gz" -Algorithm SHA256
}
finally {
    docker compose --env-file deployment.env -f compose.verified.yaml start maxkb
}
```

如果使用网络覆盖文件，也保存该文件。另行保管 MetaAgent 的私有连接配置和导入资料原件。
不要执行 `docker compose down -v` 或清理数据卷作为普通重启步骤。

在 Linux 服务器可使用等价的 Bash 冷备步骤，同样需要维护窗口：

```bash
set -e
umask 077
cd /opt/maxkb-native
maxkb_container_id="$(docker compose --env-file deployment.env -f compose.verified.yaml ps -q maxkb)"
test -n "$maxkb_container_id"
maxkb_volume="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/opt/maxkb"}}{{.Name}}{{end}}{{end}}' "$maxkb_container_id")"
test -n "$maxkb_volume"
maxkb_image="$(docker inspect --format '{{.Config.Image}}' "$maxkb_container_id")"
maxkb_backup_dir="/opt/maxkb-native/backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$maxkb_backup_dir"
docker compose --env-file deployment.env -f compose.verified.yaml stop -t 120 maxkb
trap 'docker compose --env-file deployment.env -f compose.verified.yaml start maxkb' EXIT
docker run --rm --network none --read-only --entrypoint tar \
  --mount "type=volume,source=$maxkb_volume,target=/source,readonly" \
  --mount "type=bind,source=$maxkb_backup_dir,target=/backup" \
  "$maxkb_image" -C /source -czf /backup/maxkb-data.tar.gz .
cp -a compose.verified.yaml deployment.env runtime workflow "$maxkb_backup_dir/"
sha256sum "$maxkb_backup_dir/maxkb-data.tar.gz"
docker compose --env-file deployment.env -f compose.verified.yaml start maxkb
trap - EXIT
```

### 10.3 恢复到新卷验证

恢复使用与备份相同的镜像、启动文件和配置。先创建一个不存在的新卷，保留旧卷供回退：

```powershell
$maxkbRestoreVolume = 'maxkb_native_restore_20261110'
docker volume inspect $maxkbRestoreVolume 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) { throw '恢复目标卷已存在，请选择全新的卷名' }
docker volume create $maxkbRestoreVolume
if ($LASTEXITCODE -ne 0) { throw '创建恢复卷失败' }
docker run --rm --network none --read-only --entrypoint tar --mount "type=volume,source=$maxkbRestoreVolume,target=/target" --mount "type=bind,source=$maxkbBackupDir,target=/backup,readonly" $maxkbImage -C /target -xzf /backup/maxkb-data.tar.gz
if ($LASTEXITCODE -ne 0) { throw '恢复失败，不启动业务' }
```

这里复用上一节的备份路径和镜像变量；在新终端必须先重新指定它们。
Linux 对应的新卷恢复命令如下，同样复用前一节的备份路径和镜像变量：

```bash
set -e
maxkb_restore_volume="maxkb_native_restore_$(date +%Y%m%d-%H%M%S)"
if docker volume inspect "$maxkb_restore_volume" >/dev/null 2>&1; then
  printf '恢复目标卷已存在，请换一个新卷名\n' >&2
  exit 1
fi
docker volume create "$maxkb_restore_volume"
docker run --rm --network none --read-only --entrypoint tar \
  --mount "type=volume,source=$maxkb_restore_volume,target=/target" \
  --mount "type=bind,source=$maxkb_backup_dir,target=/backup,readonly" \
  "$maxkb_image" -C /target -xzf /backup/maxkb-data.tar.gz
```

创建 restore.env，使用该新卷和独立端口，例如 18093；保留正确的两个历史文件路径。
从原部署文件启动独立项目，避免替换当前容器：

```text
docker compose -p maxkb-native-restore --env-file restore.env -f compose.verified.yaml up -d maxkb
```

验证账号、模型、文档、标签、应用 Key、检索结果和 trace；恢复演练成功后，再计划业务地址切换。
恢复失败时停止新项目，原项目和旧卷保持可用。当前尚未执行本手册的冷备恢复演练。

### 10.4 升级

先完成冷备和恢复验证，在独立卷上升级并验证两个挂载文件与新版本接口兼容。
版本升级可能执行数据库迁移，不能假定把镜像标签改回去就能安全回滚；回滚应使用升级前备份恢复的新卷。
本文不覆盖 v1 到 v2 的迁移，不能复用 v1 数据目录直接启动本版本。

## 11 排错顺序

| 现象 | 优先检查 |
| --- | --- |
| 文件挂载失败或出现 is a directory | 两个 runtime 文件是否真实存在，路径是否按 Compose 文件目录解析 |
| 启动报 bad interpreter 或权限错误 | start-maxkb.sh 是否为 LF 换行、Linux 上是否可执行 |
| 容器运行但管理页打不开 | 端口冲突、绑定地址、健康状态和启动日志 |
| 重建后知识库变空 | 实际 `/opt/maxkb` 挂载是否误换成新卷；不要删除原卷 |
| 容器重启策略仍为 no | 检查实际 RestartPolicy；不能只看 YAML |
| 页面正常但向量化失败 | 模型服务连通性、凭据、模型名、维度和文档索引状态 |
| MetaAgent 连接失败 | 容器回环地址、共享网络、服务别名及原生应用 URL |
| HTTP 成功但不是 v1 证据 | 是否调用了普通聊天应用、是否发布了正确的 Reply 工作流 |
| knowledge_access_denied | 请求 space_id、应用绑定、文档标签和应用 Key 是否一致 |
| 没有资料或召回错误 | 先看标签和原文分块，再看候选范围、阈值、重排和 trace |
| 401/403 或明确服务错误 | 区分应用 Key、管理 token 和模型 Key，不转成无资料 |

## 12 本次交付与验证范围

- 当前本机部署保持原状，未重建、未调整重启策略、未切换业务服务。
- 新增两份单容器 Compose 模板，分别用于复现已有方式和隔离验证纯官方镜像。
- 文档命令与固定版本、原生 API 和现有客户端核对；新模板执行 Compose 静态解析检查。
- 既有召回实测结果见 [验证记录](maxkb-native-verification-20261110.md)。
- 新服务器部署、纯官方模型组合、冷备恢复、业务切换仍需按本文在目标环境执行验收。

相关文件：[部署模板与节点说明](../deployment/maxkb/README.md)、[完整过程示例](maxkb-native-retrieval-implementation.md)、
[客户端接入契约](maxkb-knowledge-search-v1.md)。
