# Docker Desktop 启动故障排查与恢复

日期：2026-10-18，时间均为北京时间。

## 已确认的原因

Docker Desktop 4.90.0 启动时，内部 Ingest 服务处理残留 AF_UNIX socket 失败：

```text
initializing Ingest server
sailor-ingest.sock -> sailor-ingest.sock.stale
The file cannot be accessed by the system.
```

后端随后退出，`dockerDesktopLinuxEngine` 命名管道没有创建，Docker API 无法连接。
这是本次启动失败的直接原因。socket 最初为何残留尚未确认；不能把所有历史容器退出都归为同一原因。

证据来自 `%LOCALAPPDATA%\Docker\log\host\com.docker.backend.exe.log` 及其归档。
日志里今天 10:19 已有同样错误，早于本轮调用；当前启动在 22:20:03 报错、22:20:25 记录 backend crashed。
运行目录中相关 socket 与 `.stale` 对象都是 0 字节 ReparsePoint，最后写入时间早于本轮实验。

## 本轮调用时间线

1. 首次 `docker ps -a` / `docker image ls` 只读查询即报告引擎管道不存在。
2. 执行官方 `docker desktop start` 尝试启动；这次启动触发了用户看到的报错。
3. 在任何实验容器创建命令之前，后端已经因上述 socket 改名错误退出。
4. 排查时没有 Docker 后端／桌面进程，唯一 WSL 分发 docker-desktop 为 Stopped。
   宿主可用物理内存约 16.38 GiB；没有据此证明所有历史 OOM 均不存在。
5. 读取日志和系统状态后才进行下面的一次定向恢复。

`docker desktop start` 本身是官方支持的启动方式。
后续改用 `docker desktop start --detach`，并以 Docker API 判定就绪，避免无限同步等待；
后台参数本身不修复 socket 故障，本次恢复来自对残留运行目录的隔离。

## 已执行的恢复

确认所有 Docker 进程停止后，检查并改名保留了两个运行目录：

| 原目录，相对 %LOCALAPPDATA% | 保留目录 |
|---|---|
| Docker\run | Docker\run.quarantine-20261018-222337 |
| docker-secrets-engine | docker-secrets-engine.quarantine-20261018-222337 |

第二个目录经检查只有 `engine.sock` 和 `engine.sock.stale` 两个零字节 socket 对象。
移动前验证源／目标绝对路径及父目录，使用 PowerShell `Move-Item -LiteralPath`，
没有遍历删除 socket，也没有跨 Shell 拼接文件操作。
随后创建同名空运行目录，并执行一次 `docker desktop start --detach`。

没有执行 Factory Reset、WSL unregister、prune、VHDX 恢复、镜像／卷删除或配置重写。
镜像、数据卷及 Docker 的 WSL 存储没有被本次修复脚本删除或重置。
操作清单保存在项目私有目录 `.local/docker-recovery/20261018-222337.json`。

## 恢复结果

- Docker API 正常返回，Engine 版本 29.7.2。
- 原有重启策略自动恢复 7 个容器；44 个既有容器仍可枚举。
- MaxKB 健康检查通过，RestartCount=0、OOMKilled=false。
- 后续单个隔离 rerank 实验正常退出且未 OOM；实验容器已移除，回到 44 个容器／7 个运行。
- 没有重建或切换本机旧 Agent／bridge，也没有更改它们的模型配置。

这确认当前已恢复，不代表 Docker Desktop 的底层问题已经永久修复。
如再次出现，先记录具体失败 socket 和后端日志，不能仅凭“闪退”认定是容器过多，
也不应把镜像删除当作这类启动错误的修复步骤。

## 依据与后续调用方式

官方支持 [Desktop CLI 启停](https://docs.docker.com/desktop/features/desktop-cli/) 和
[`start --detach`](https://docs.docker.com/reference/cli/docker/desktop/start/)。
Docker 官方反馈仓库已有[与本机日志吻合的同类报告](https://github.com/docker/desktop-feedback/issues/666)；
该报告支持现象比对，不等于厂商确认了本机最初残留 socket 的成因。

后续使用单次后台启动、就绪后再运行任务；实验只启动明确命名且设置资源上限的容器。
正常停止使用 Desktop 的正常退出路径，保留本次隔离目录作为恢复记录。
没有自动升级 Docker、重启 Windows，或创建后台监控任务。
