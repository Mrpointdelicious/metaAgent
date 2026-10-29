# 交互聊天接口兼容

工具及知识库迁移提交 `81507d3` 已快进合入本地 main，并保留 master 的 `7249dce`、`0620db1`。
本次在服务端适配已有 `interactive.py`；启动脚本、交互脚本及实际环境配置内容保持不变。

## 请求与响应

| 入口 | 请求格式 | 阻塞返回 |
| --- | --- | --- |
| `/v1/chat`，有 `request_id` | 原有 NativeChatRequest，严格验证字段 | 原生运行快照与事件 |
| `/v1/chat`，没有 `request_id` | 交互客户端文本请求 | `answer`、`task_id`、`message_id`、`conversation_id` |
| `/v1/chat-messages` | 文本聊天兼容格式 | 同上 |
| `/compat/dify/v1/chat-messages` | 文本聊天兼容格式 | 同上 |

兼容请求示例：

```json
{
  "user": "interactive-cli",
  "inputs": {"projectPatientId": "3799"},
  "query": "你好",
  "conversation_id": "",
  "response_mode": "blocking"
}
```

省略请求编号时，每次调用生成新编号和 task ID；首次响应返回新会话 ID，续聊传回此 ID。
相同文字的新一轮不会因内容相同而被去重。`task_id` 对应原生 run ID，可直接用于已有的
`POST /v1/runs/{task_id}/status` 和 `/cancel`，鉴权及用户、租户、角色、患者作用域校验共用原有逻辑。

两个 `chat-messages` 入口也支持显式传入有效的 `request_id`，用于已有请求幂等机制。
同编号同内容返回原运行；同编号不同内容返回 409。运行中复用返回 202，已结束复用返回 200。
重复请求即使选择 streaming 也返回 JSON 快照，不再播放事件或场景动作。
`/v1/chat` 传入空、空白或 null 请求编号仍返回 422，不会降级到兼容格式。

阻塞兼容响应保留 `event=message`、`mode=chat`、时间戳与稳定消息 ID。
`metadata` 包含运行状态、请求编号、工具/模型调用数、知识来源和报表信息。
实际业务失败由运行状态及说明文字表达，不能只根据 HTTP 200 判断业务是否成功。

## 手机号与交互脚本

`patientphone`、`patientPhone`、`patient_phone` 都会通过同一患者身份解析接口转换为可信患者身份。
多个别名值不一致时返回 422；项目患者编号与手机号的互斥校验仍由身份解析接口执行。
问题文本中的患者编号或手机号不会被当作身份输入。

```powershell
python interactive.py http://localhost:8080 -i 3799 -v
python interactive.py http://localhost:8080 -i phone:13800000000 -v
```

服务令牌仍通过 `-t`、既有进程变量或已有 `.env` 读取。Docker 端口映射部署时可将地址改为
`http://localhost:5080`。`new` 创建新会话，`exit` 结束交互；`-v` 使用返回的 task ID 获取原生状态及指标。

## 流式行为

`response_mode=streaming` 返回 SSE。兼容格式先消费原生进度和工具事件，完成后发送一条最终
`message`，随后发送 `message_end`；等待期间每 15 秒发送 `event: ping` 保活。
同一目标的新版回答替换旧版，避免把原生修订误当成增量文本而重复展示。
整轮失败或取消时发送 `error` 和携带终态的 `message_end`。
报表以已验证的图片链接展示；场景动作不会因文本流被消费而标记为已派发。

断线或取消等待时取消原始 application run 及其进行中的工具调用。
原生格式的 SSE 事件顺序和动作交付机制继续保持原行为。

## 范围

这是文本聊天和现有状态/取消接口的兼容实现。`files=[]` 可接受，非空附件返回 422。
Workflow、会话列表和历史消息查询尚未提供。
旧脚本可透传的 `pos`、`requestphone`、`companykb`、`apikey`、`apitoken` 不会启用额外的问诊或病历写入流程。
知识源和凭据通过服务端 `.env` 配置，旧输入字段不会覆盖这些配置。

回归包含原生协议、三条兼容入口、会话和幂等行为、身份别名、流式错误/断线，以及使用未修改
`interactive.py` 连接真实本机 HTTP 服务的项目编号/手机号连续对话与详细指标查询。
患者后端在测试中使用合成身份和数据。

本地及 Docker test 镜像完整回归均为 249 项通过、1 项因未配置测试 PostgreSQL 跳过。
修改文件的 Ruff 检查和 `git diff --check` 通过。
