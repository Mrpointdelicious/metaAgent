"""
创建日期：2026-09-08
文件功能：查询当前空间在线医生 + 解析用户挂号/选择，直连上游服务并落证据库。

对应 Dify 代码节点 main(space_id, user_query)：
  登录拿 token -> GET /api/users?spaceId=.. 查在线医生 -> 按 user_query 解析选择
  -> 输出 command（[DoctorRecommend:]手机号）与 patient_message。
本模块把该节点直连实现搬到 MetaAgent（不再走 ctx.call 后端代理）。

日志约定：只记录不含个人数据的诊断字段（run_id/event_type/doctor_count/error/状态码等），
不打印 token、医生姓名或手机号；便于前端上线后按 run_id 串联排查。
"""

import json
import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from meta_agent.application.context import RunContext
from meta_agent.contracts import TaskResult, TaskSpec

logger = logging.getLogger(__name__)

# 与后端 DoctorToolsService 保持一致的硬编码地址/账号
LOGIN_URL = "http://www.ga-robot.com/webyun/api/Login/Login"
QUERY_URL = "http://192.168.8.71:4000/api/users"
LOGIN_PHONE = "15932910402"
LOGIN_PASSWORD = "25d55ad283aa400af464c76d713c07ad"
TIMEOUT = 15  # 秒，对应 C# RequestTimeoutSeconds

# 输出契约常量（与患者工具参考实现保持一致）
TOOL_KIND = "doctors"
CONTRACT_VERSION = "504-compact-v1"

# 中文数字 → 阿拉伯数字（解析"第一个/第二个"这类选择）
CN_NUM = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _extra(ctx: RunContext, event_type: str, **fields: Any) -> dict[str, Any]:
    """构造日志 extra：run_id 用于串联，fields 携带结构化诊断字段。"""
    return {
        "run_id": ctx.record.run_id,
        "event_type": event_type,
        "dependency": "doctor-upstream",
        "fields": {"tool": "search_doctors", **fields},
    }


def _json(value: Any) -> str:
    """紧凑 JSON（无空格），与参考实现一致。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _extract_token(raw: str) -> str:
    """从登录响应里按多条路径提取 token（对应 C# ExtractToken）。"""
    try:
        doc = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(doc, dict):
        return ""
    for path in (
        ("token",),
        ("data", "token"),
        ("data", "data", "token"),
        ("body", "token"),
        ("body", "data", "token"),
        ("body", "data", "data", "token"),
    ):
        cur = doc
        ok = True
        for seg in path:
            if isinstance(cur, dict) and seg in cur:
                cur = cur[seg]
            else:
                ok = False
                break
        if ok and isinstance(cur, str) and cur.strip():
            return cur.strip()
    return ""


def _decode_bytes(content: bytes) -> str:
    """按 UTF-8 解码，出现替换符则回退 GBK（对应 C# DecodeResponse）。"""
    text = content.decode("utf-8", errors="replace")
    if "�" not in text:
        return text
    try:
        return content.decode("gbk")
    except Exception:
        return text


def _doctor_label(name: Any) -> str:
    """姓名 → 医生称呼：'姚雄'→'姚雄医生'，已含'医生'的保持不变（如'医生5'）。"""
    name = str(name or "").strip()
    if not name:
        return "医生"
    return name if "医生" in name else name + "医生"


def _select_indices(query: str, names: list[str]) -> list[int]:
    """从 user_query 判断用户选中的医生序号（1-based 升序）。"""
    q = (query or "").strip()
    if not q:
        return []
    # 全选
    if any(w in q for w in ("全部", "所有", "都选", "都要", "全选")):
        return list(range(1, len(names) + 1))
    idx: list[int] = []
    # "第一个 / 第1个 / 第二"
    for m in re.finditer(r"第\s*([一二两三四五六七八九十]|\d+)", q):
        s = m.group(1)
        idx.append(CN_NUM.get(s) or int(s))
    # "选1 / 选择二 / 要第三个"
    for m in re.finditer(r"(?:选|选择|要|想要)\s*([一二两三四五六七八九十]|\d+)", q):
        s = m.group(1)
        idx.append(CN_NUM.get(s) or int(s))
    # 直接点名：医生姓名出现在 query 里
    for i, name in enumerate(names, 1):
        if name and name in q:
            idx.append(i)
    # 兜底：纯数字
    if not idx:
        for m in re.finditer(r"\d+", q):
            idx.append(int(m.group(0)))
    return sorted(set(idx))


def _join_names(items: list[str]) -> str:
    """张三、李四 → 张三和李四；张三、李四、王五 → 张三、李四和王五。"""
    if len(items) <= 1:
        return "、".join(items)
    return "、".join(items[:-1]) + "和" + items[-1]


def _base() -> dict[str, str]:
    return {
        "success": "false",
        "status": "failed",
        "result": "{}",
        "doctor_count": "0",
        "doctor_names": "",
        "command": "",
        "patient_message": "",
        "status_code": "",
        "error": "",
    }


# 输出变量集合（顺序与参考实现最终 return 一致）
_OUTPUT_KEYS = [
    "success", "status", "needs_clarification", "clarification_question",
    "result", "image_url", "artifact_ref", "patient_message", "status_code",
    "error", "command", "tool_kind", "contract_version", "query_context",
    "has_instruction", "match_name", "intent_type", "doctor_count", "doctor_names",
]


def _wrap(output: dict[str, Any]) -> dict[str, str]:
    """补齐框架字段，返回与参考实现一致的 19 个输出变量。"""
    out = dict(output)
    out.update(tool_kind=TOOL_KIND, contract_version=CONTRACT_VERSION)
    # 失败时不携带任何成功态 URL/指令
    if str(out.get("success", "")).lower() == "false" or out.get("error"):
        out.update(status="failed", image_url="", artifact_ref="", command="")
    return {k: out.get(k, "") for k in _OUTPUT_KEYS}


async def _login(client: httpx.AsyncClient, ctx: RunContext) -> tuple[str, str]:
    """登录拿 token，返回 (token, error_token)；error_token 为空表示成功。"""
    try:
        resp = await client.post(
            LOGIN_URL,
            json={"Phone": LOGIN_PHONE, "Password": LOGIN_PASSWORD},
            timeout=TIMEOUT,
        )
    except httpx.TimeoutException:
        logger.warning(
            "doctor login timeout", extra=_extra(ctx, "doctor_login_timeout", error="login_timeout")
        )
        return "", "login_timeout"
    except httpx.RequestError:
        logger.warning(
            "doctor login connection error",
            extra=_extra(ctx, "doctor_login_connection", error="login_connection_error"),
        )
        return "", "login_connection_error"
    if resp.status_code >= 400:
        logger.warning(
            "doctor login http error",
            extra=_extra(ctx, "doctor_login_http", status_code=resp.status_code),
        )
        return "", f"login_http_{resp.status_code}"
    token = _extract_token(_decode_bytes(resp.content))
    if not token:
        logger.warning(
            "doctor login missing token", extra=_extra(ctx, "doctor_login_no_token")
        )
        return "", "login_no_token"
    logger.info("doctor login ok", extra=_extra(ctx, "doctor_login_ok"))
    return token, ""


async def _query_doctors(ctx: RunContext, user_query: str) -> dict[str, str]:
    """搜索在线医生 + 判断用户选择的医生，返回与参考实现一致的输出变量。"""
    output = _base()
    sid = (ctx.scope.space_id or "").strip()
    if not sid:
        logger.warning(
            "doctor query skipped", extra=_extra(ctx, "doctor_missing_space", error="missing_space_id")
        )
        output["error"] = "missing_space_id"
        output["patient_message"] = "在线医生信息暂时无法读取，请稍后重试。"
        return _wrap(output)

    token, login_err = await _login(ctx.backend.client, ctx)
    if login_err:
        output["error"] = login_err
        output["patient_message"] = "在线医生信息暂时无法读取，请稍后重试。"
        return _wrap(output)

    sep = "&" if "?" in QUERY_URL else "?"
    url = QUERY_URL + sep + "spaceId=" + quote(sid, safe="")
    try:
        resp = await ctx.backend.client.get(
            url,
            headers={"Authorization": "Bearer " + token},
            timeout=TIMEOUT,
        )
    except httpx.TimeoutException:
        logger.warning(
            "doctor query timeout", extra=_extra(ctx, "doctor_query_timeout", error="timeout")
        )
        output["error"] = "timeout"
        output["patient_message"] = "在线医生查询超时，请稍后重试。"
        return _wrap(output)
    except httpx.RequestError:
        logger.warning(
            "doctor query connection error",
            extra=_extra(ctx, "doctor_query_connection", error="connection_error"),
        )
        output["error"] = "connection_error"
        output["patient_message"] = "在线医生查询接口暂时不可用，请稍后重试。"
        return _wrap(output)

    output["status_code"] = str(resp.status_code)
    logger.info(
        "doctor query http",
        extra=_extra(ctx, "doctor_query_http", status_code=resp.status_code),
    )
    if resp.status_code >= 400:
        output["error"] = f"query_http_{resp.status_code}"
        output["patient_message"] = "在线医生查询接口暂时不可用，请稍后重试。"
        return _wrap(output)

    try:
        doc = json.loads(_decode_bytes(resp.content))
    except Exception:
        logger.warning(
            "doctor query invalid json", extra=_extra(ctx, "doctor_query_invalid_json", error="invalid_json")
        )
        output["error"] = "invalid_json"
        output["patient_message"] = "在线医生信息暂时无法读取，请稍后重试。"
        return _wrap(output)
    if not isinstance(doc, dict):
        logger.warning(
            "doctor query invalid payload",
            extra=_extra(ctx, "doctor_query_invalid_payload", error="invalid_payload"),
        )
        output["error"] = "invalid_payload"
        output["patient_message"] = "在线医生信息暂时无法读取，请稍后重试。"
        return _wrap(output)

    message = (doc.get("message") or "").strip()
    data = doc.get("data")
    if message == "暂无在线医生" or not isinstance(data, list):
        data = []

    names, phones = [], []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
        phone = item.get("phone")
        if isinstance(phone, str) and phone.strip():
            phones.append(phone.strip())

    count = len(names)
    output["doctor_count"] = str(count)
    output["doctor_names"] = "、".join(names)

    # 默认（纯搜索，user_query 为空）话术与指令
    if count == 0:
        status = "empty"
        command = ""
        patient_message = "当前没有在线医生，请稍后再试。"
    elif count == 1:
        status = "available"
        command = ("[DoctorRecommend:]" + phones[0]) if phones else ""
        patient_message = "当前只有%s在线%s。" % (
            names[0],
            "，已为您挂号该医生" if command else "",
        )
    else:
        status = "available"
        command = ""
        patient_message = "当前有多个在线医生，分别是%s，请选择要挂号的医生。" % _join_names(names)

    # 用户明确选了医生 → 覆盖成对应挂号指令 + 回复
    selected = [i for i in _select_indices(user_query, names) if 1 <= i <= count]
    if selected and count > 0:
        i = selected[0] - 1
        command = ("[DoctorRecommend:]" + phones[i]) if i < len(phones) and phones[i] else ""
        patient_message = "好的，已为您挂号%s。" % _doctor_label(names[i])

    output["success"] = "true"
    output["status"] = status
    output["command"] = command
    output["patient_message"] = patient_message
    output["result"] = _json({
        "status": status,
        "doctor_count": count,
        "doctor_names": names,
        "has_command": bool(command),
    })

    logger.info(
        "doctor query done",
        extra=_extra(
            ctx,
            "doctor_query_done",
            doctor_count=count,
            status=status,
            has_command=bool(command),
            selected=bool(selected),
        ),
    )
    return _wrap(output)


class DoctorAdapter:
    """直连上游查在线医生并解析挂号选择，落证据库并投影为 TaskResult。"""

    async def execute(self, task: TaskSpec, args: dict[str, Any], ctx: RunContext) -> TaskResult:
        del args
        # 直连上游也算一次工具调用，与走 ctx.call 的领域适配器保持一致，
        # 计入 tool_calls 指标与调用预算。
        ctx.tool_calls += 1
        out = await _query_doctors(ctx, ctx.query or "")
        evidence = await ctx.repository.save_evidence(
            ctx.scope.scope_hash, "search_doctors", ctx.record.request_id, out, CONTRACT_VERSION
        )
        success = str(out.get("success", "")).lower() == "true"
        count = int(out.get("doctor_count") or "0")
        ctx.record.metrics["doctor_count"] = out.get("doctor_count", "")
        ctx.record.metrics["doctor_has_command"] = bool(out.get("command", ""))
        return TaskResult(
            task_id=task.task_id,
            status="succeeded" if success else "unavailable",
            message=out.get("patient_message") or "",
            evidence_ids=[evidence.evidence_id],
            outputs={
                "tool_name": "search_doctors",
                "command": out.get("command", ""),
                "doctor_count": out.get("doctor_count", ""),
                "doctor_names": out.get("doctor_names", ""),
                "status": out.get("status", ""),
                "error": out.get("error", ""),
                # 供 guard/condition 判断「有医生/没有医生」：查询成功时才给出确定结论。
                "has_results": success and count > 0,
                "no_results": success and count == 0,
                "has_instruction": out.get("has_instruction", ""),
                "match_name": out.get("match_name", ""),
                "intent_type": out.get("intent_type", ""),
                "needs_clarification": out.get("needs_clarification", ""),
                "clarification_question": out.get("clarification_question", ""),
                "contract_version": CONTRACT_VERSION,
            },
        )
