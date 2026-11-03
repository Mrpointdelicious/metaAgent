"""
创建日期：2026-10-14
文件功能：病历填写子流程完整链路。

流程（与 Dify 原工作流一致）：
  1. POST Login/Login（固定账号）→ parse_token 提取 token；
  2. GET SysUserInfo/GetUserMedicalInfomation
  New（Bearer token + Phone=patientphone）
     → parse_medical_data 提取 data（患者病历信息）；
  3. GET SysUserInfo/GetUserIdAndRole（Bearer
  token + Phone=patientphone）
     → parse_user_role 提取 UserId / TenantId；
  4. GET SysMedical/GetMedicalRecordTemplateC
  ontent（Bearer token + userId=UserId）
     → 原始响应 body（补充信息）；
  5. current_time() 生成当前时间（dateTime=YYYY/M/D）；
  6. GET /api/chat-records（Bearer token + phone=requestphone）
     → 原始响应 body（对话内容）；
  7. 用 MEDICAL_RECORD_FILL_PROMPT 调用 LLM，生成「基本信息补充」纯文本；
  8. parse_fill_result 把文本封装为
  medical_record_fill 事件，返回 result JSON 字符串。
"""

import json
import logging
import re
from datetime import datetime
from typing import Any

import httpx

from meta_agent.adapters.text import model_text
from meta_agent.application.context import RunContext
from meta_agent.contracts import DomainError
from meta_agent.observability.trace import CURRENT_RUN_ID

from .access import ConsultationClient
from .prompts import MEDICAL_RECORD_FILL_PROMPT

logger = logging.getLogger(__name__)

LOGIN_URL = "http://www.ga-robot.com/webyun/api/Login/Login"
MEDICAL_INFO_URL = "http://192.168.8.71:8085/webyun/api/SysUserInfo/GetUserMedicalInfomationNew"
USER_ROLE_URL = "http://192.168.8.71:8085/webyun/api/SysUserInfo/GetUserIdAndRole"
TEMPLATE_CONTENT_URL = (
    "http://192.168.8.71:8085/webyun/api/SysMedical/GetMedicalRecordTemplateContent"
)
CHAT_RECORDS_URL = "http://192.168.8.71:4000/api/chat-records"
TIMEOUT = 15  # 秒


def _mask_phone(value: Any) -> str:
    """手机号打码：保留前 3 位和后 2 位，中间替换为 ****。"""
    s = str(value)
    if len(s) <= 4:
        return "****"
    return s[:3] + "****" + s[-2:]


def _mask_params(params: dict[str, str]) -> dict[str, str]:
    """接口入参打码：手机号/电话类字段掩码，其余原样转字符串。"""
    masked: dict[str, str] = {}
    for key, value in params.items():
        if "phone" in key.lower() or "mobile" in key.lower():
            masked[key] = _mask_phone(value)
        else:
            masked[key] = str(value)
    return masked


# -------------------------------------------
# -------------------------------- #
# 解析函数（照搬 Dify 代码节点逻辑）
# -------------------------------------------
# -------------------------------- #


def parse_token(input_data: Any) -> dict[str, str]:
    """从登录响应中提取 token；未获取到时返回 {"token": ""}。"""
    if isinstance(input_data, str):
        try:
            input_data = json.loads(input_data)
        except Exception:
            return {"token": ""}

    if not isinstance(input_data, dict):
        return {"token": ""}

    # token 可能在顶层或 data 层
    direct_token = str(input_data.get("token", "")) if input_data.get("token") is not None else ""
    if not direct_token and isinstance(input_data.get("data"), dict):
        data_layer = input_data.get("data") or {}
        direct_token = (
            str(data_layer.get("token", "")) if data_layer.get("token") is not None else ""
        )
        if not direct_token and isinstance(data_layer.get("data"), dict):
            direct_token = str(data_layer.get("data", {}).get("token", "")) or ""
    if direct_token:
        return {"token": direct_token}

    # token 不在顶层，则尝试 body（可能是字符串或 dict）
    body_json = input_data.get("body")
    if not body_json:
        return {"token": ""}

    if isinstance(body_json, str):
        try:
            body_obj = json.loads(body_json)
        except Exception:
            return {"token": ""}
    elif isinstance(body_json, dict):
        body_obj = body_json
    else:
        return {"token": ""}

    if not isinstance(body_obj, dict):
        return {"token": ""}

    data_layer = body_obj.get("data", {})
    token = ""
    if isinstance(data_layer, dict):
        token = str(data_layer.get("token", "")) or ""
        if not token and isinstance(data_layer.get("data", {}), dict):
            token = str(data_layer.get("data", {}).get("token", "")) or ""
    if not token and body_obj.get("token") is not None:
        token = str(body_obj.get("token", "")) or ""

    return {"token": token}


def parse_medical_data(body: Any) -> dict[str, Any]:
    """从病历接口响应中提取 data 字段。"""
    if not body:
        return {"data": {}}

    body_str = body.get("body") or "" if isinstance(body, dict) else body

    try:
        level1 = json.loads(body_str)
    except Exception:
        return {"data": {}}

    data = level1.get("data", {})
    return {"data": data}


def parse_user_role(arg1: Any) -> dict[str, str]:
    """从 GetUserIdAndRole 响应中提取 UserId / TenantId。"""
    data = json.loads(arg1) if isinstance(arg1, str) else arg1
    inner = data.get("data", {}) if isinstance(data, dict) else {}
    return {
        "UserId": str(inner.get("UserId", "")),
        "TenantId": str(inner.get("TenantId", "")),
    }


def _today() -> Any:
    # 如需固定东八区可改为：datetime.utcnow() + timedelta(hours=8)
    return datetime.now().date()


def _norm_slashes(s: str) -> str:
    """把 YYYY-M-D / YYYY.M.D / YYYY年M月D日 等格式统一成 YYYY/M/D。"""
    s = s.strip()
    s = re.sub(r"[年\-.]", "/", s)
    s = re.sub(r"[月]", "/", s)
    s = re.sub(r"[日号]", "", s)
    s = re.sub(r"/+", "/", s)
    return s.strip("/")


def current_time() -> dict[str, str]:
    """仅返回「今天」的日期，不做任何解析。"""
    today = _today()
    y, m, d = today.year, today.month, today.day

    return {
        "mode": "day",
        "year": str(y),
        "month": str(m),
        "day": str(d),
        "dateTime": _norm_slashes(f"{y}/{m}/{d}"),  # "YYYY/M/D"
        "plan_select": "true",
    }


def _clean_value(v: Any) -> str:
    """
    去掉值末尾「←系统已有 / ←默认值 / ←AI推断 / ←历史记录沿用 /
    ←对话提取」等来源标注，只保留真实值。
    """
    return (v or "").split("←")[0].strip()


def _format_birthdate(v: Any) -> str:
    """把出生日期归一化成 YYYYMMDD（8 位数字），如 1980年1月1日 → 19800101。"""
    v = (v or "").strip()
    if not v:
        return v
    if re.fullmatch(r"\d{8}", v):
        return v
    m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", v)
    if m:
        return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    return v


_INCOME_ENUM = ("<5万元", "5~10万元", "10~20万", "20~50万元", "50万元以上")


def _income_key(s: Any) -> str:
    """归一化收入字符串用于比较：去空格、去「元」、把各种区间/连接符统一成 ~。"""
    s = (s or "").replace(" ", "").replace("元", "")
    return re.sub(r"[-–—~～至到]", "~", s)


def _normalize_income(v: Any) -> str:
    """把家庭年收入归一化成固定枚举；无法识别返回空串。"""
    v = (v or "").strip()
    if not v:
        return ""

    key = _income_key(v)
    # 1) 与枚举精确匹配（兼容 5-10万 / 5到10万 / 50万以上 等写法）
    for e in _INCOME_ENUM:
        if key == _income_key(e):
            return e

    # 2) 抽取数字分档
    nums = re.findall(r"\d+(?:\.\d+)?", v)
    if not nums:
        return ""

    has_wan = "万" in v
    n = float(nums[0])
    if not has_wan and n >= 1000:
        n /= 10000.0  # 无「万」的大数按「元」折算成万元

    if n < 5:
        return "<5万元"
    if n < 10:
        return "5~10万元"
    if n < 20:
        return "10~20万"
    if n < 50:
        return "20~50万元"
    return "50万元以上"


def parse_fill_result(arg1: Any, arg2: Any, arg3: Any) -> dict[str, str]:
    """将病历代写结果封装为「病历填写」事件。

    arg1: 病历信息（PatientDetails，暂不使用，预留）
    arg2: 补充信息（basic_info_supplement，暂不使用，预留）
    arg3: 病历代写文本（"基本信息补充·AI一键代写..."）
    输出 result: JSON 字符串
    {"event":"medical_record_fill","display_t
    ext":"[Answer:]"+arg3,"payload":{19个字段}}。
    """
    fill_text = arg3 if isinstance(arg3, str) else ""

    # 字段中文名 -> payload 英文名（顺序与 basic_info_supplement 一致）
    FIELD_MAP = [
        ("姓名", "PatientName"),
        ("病史采集时间", "HistoryCollectionTime"),
        ("出生日期", "BirthDate"),
        ("年龄", "Age"),
        ("民族", "Ethnicity"),
        ("性别", "Gender"),
        ("籍贯", "NativePlace"),
        ("工作单位", "WorkUnit"),
        ("住址", "Address"),
        ("利手", "Handedness"),
        ("教育程度", "EducationLevel"),
        ("就业情况", "EmploymentStatus"),
        ("婚姻状况", "MaritalStatus"),
        ("结婚年龄", "MarriageAge"),
        ("健康状况", "HealthStatus"),
        ("居住情况", "LivingSituation"),
        ("家庭年收入", "AnnualFamilyIncome"),
        ("病史提供者", "HistoryProvider"),
        ("可靠程度", "Reliability"),
    ]

    payload = {en: "" for _, en in FIELD_MAP}

    for line in fill_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("←"):
            continue
        if line.startswith("数据来源标注"):
            break
        if line.startswith("基本信息补充") or line.startswith("[确认]"):
            continue

        # 特殊行：出生日期：X → 年龄：Y
        if line.startswith("出生日期："):
            rest = line[len("出生日期：") :]
            if "→" in rest:
                birth, age_part = rest.split("→", 1)
                payload["BirthDate"] = _format_birthdate(_clean_value(birth))
                age_part = age_part.strip()
                if age_part.startswith("年龄："):
                    payload["Age"] = _clean_value(age_part[len("年龄：") :])
            else:
                payload["BirthDate"] = _format_birthdate(_clean_value(rest))
            continue

        # 通用字段：字段名：值
        for cn, en in FIELD_MAP:
            if cn in ("出生日期", "年龄"):
                continue
            if line.startswith(cn + "："):
                payload[en] = _clean_value(line[len(cn) + 1 :])
                break

    # 家庭年收入：归一化成固定枚举
    payload["AnnualFamilyIncome"] = _normalize_income(payload["AnnualFamilyIncome"])

    event = {
        "event": "medical_record_fill",
        "display_text": "[Answer:]" + fill_text + "\n",
        "payload": payload,
    }
    return {"result": json.dumps(event, ensure_ascii=False)}


# -------------------------------------------
# -------------------------------- #
# 网络请求与编排
# -------------------------------------------
# -------------------------------- #


def _phone(ctx: RunContext, *keys: str) -> str:
    access = ctx.consultation_access
    if access is None:
        raise DomainError("consultation_forbidden", "请先确认问诊参与关系。", outcome="unavailable")
    return access.requester_phone if "requesterphone" in keys else access.patient_phone


def _to_text(value: Any) -> str:
    """把任意值转成可写入提示词的文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _extract_content(response: Any) -> str:
    content = response.content if hasattr(response, "content") else str(response)
    if isinstance(content, list):
        content = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return model_text(content or "")


async def _login(client: Any, settings) -> str:
    run_id = CURRENT_RUN_ID.get()
    try:
        resp = await client.post(
            LOGIN_URL,
            json={
                "Phone": settings.webyun_login_phone,
                "Password": settings.webyun_login_password.get_secret_value(),
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        login_body = resp.json()
    except httpx.TimeoutException as exc:
        logger.warning(
            "medical record login timeout",
            extra={
                "run_id": run_id,
                "event_type": "http_call",
                "fields": {"step": "login", "method": "POST", "url": LOGIN_URL, "error": "timeout"},
            },
        )
        raise DomainError(
            "medical_record_login_timeout", "病历服务登录超时。", outcome="unavailable"
        ) from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        body = exc.response.text[:300] if exc.response is not None else ""
        logger.warning(
            "medical record login http error status=%s body=%s",
            status,
            body,
            extra={
                "run_id": run_id,
                "event_type": "http_call",
                "fields": {
                    "step": "login",
                    "method": "POST",
                    "url": LOGIN_URL,
                    "status": status,
                    "body_preview": body,
                },
            },
        )
        raise DomainError(
            "medical_record_login_http", "病历服务登录失败。", outcome="unavailable"
        ) from exc
    except httpx.RequestError as exc:
        logger.warning(
            "medical record login connection error err=%s",
            type(exc).__name__,
            extra={
                "run_id": run_id,
                "event_type": "http_call",
                "fields": {
                    "step": "login",
                    "method": "POST",
                    "url": LOGIN_URL,
                    "error": type(exc).__name__,
                },
            },
        )
        raise DomainError(
            "medical_record_login_connection", "病历服务登录失败。", outcome="unavailable"
        ) from exc

    token = parse_token(login_body).get("token", "")
    logger.info(
        "login ok status=%s",
        resp.status_code,
        extra={
            "run_id": run_id,
            "event_type": "http_call",
            "fields": {
                "step": "login",
                "method": "POST",
                "url": LOGIN_URL,
                "status": resp.status_code,
                "has_token": bool(token),
            },
        },
    )
    if not token:
        raise DomainError(
            "medical_record_token_missing", "未获取到病历服务访问凭证。", outcome="unavailable"
        )
    return token


async def _get(
    client: Any, url: str, *, token: str, params: dict[str, str], step: str, label: str
) -> httpx.Response:
    run_id = CURRENT_RUN_ID.get()
    masked_params = _mask_params(params)
    try:
        resp = await client.get(
            url,
            headers={"Authorization": "Bearer " + token},
            params=params,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.TimeoutException as exc:
        logger.warning(
            "%s timeout url=%s",
            step,
            url,
            extra={
                "run_id": run_id,
                "event_type": "http_call",
                "fields": {
                    "step": step,
                    "method": "GET",
                    "url": url,
                    "params": masked_params,
                    "error": "timeout",
                },
            },
        )
        raise DomainError(f"{step}_timeout", f"{label}超时。", outcome="unavailable") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        body = exc.response.text[:300] if exc.response is not None else ""
        logger.warning(
            "%s http error url=%s status=%s body=%s",
            step,
            url,
            status,
            body,
            extra={
                "run_id": run_id,
                "event_type": "http_call",
                "fields": {
                    "step": step,
                    "method": "GET",
                    "url": url,
                    "params": masked_params,
                    "status": status,
                    "body_preview": body,
                },
            },
        )
        raise DomainError(
            f"{step}_http", f"{label}接口返回异常（HTTP {status}）。", outcome="unavailable"
        ) from exc
    except httpx.RequestError as exc:
        logger.warning(
            "%s connection error url=%s err=%s",
            step,
            url,
            type(exc).__name__,
            extra={
                "run_id": run_id,
                "event_type": "http_call",
                "fields": {
                    "step": step,
                    "method": "GET",
                    "url": url,
                    "params": masked_params,
                    "error": type(exc).__name__,
                },
            },
        )
        raise DomainError(
            f"{step}_connection", f"{label}接口暂时不可用。", outcome="unavailable"
        ) from exc

    logger.info(
        "%s ok url=%s status=%s",
        step,
        url,
        resp.status_code,
        extra={
            "run_id": run_id,
            "event_type": "http_call",
            "fields": {
                "step": step,
                "method": "GET",
                "url": url,
                "params": masked_params,
                "status": resp.status_code,
                "body_preview": resp.text[:800],
            },
        },
    )
    return resp


async def _get_medical_info(client: Any, token: str, phone: str) -> dict[str, Any]:
    resp = await _get(
        client,
        MEDICAL_INFO_URL,
        token=token,
        params={"Phone": phone},
        step="medical_record",
        label="病历信息查询",
    )
    return parse_medical_data(resp.text).get("data", {})


async def _get_user_role(client: Any, token: str, phone: str) -> dict[str, str]:
    resp = await _get(
        client,
        USER_ROLE_URL,
        token=token,
        params={"Phone": phone},
        step="user_role",
        label="用户角色查询",
    )
    return parse_user_role(resp.text)


async def _get_template_content(client: Any, token: str, user_id: str) -> str:
    resp = await _get(
        client,
        TEMPLATE_CONTENT_URL,
        token=token,
        params={"userId": user_id},
        step="template_content",
        label="病历模板查询",
    )
    return resp.text


async def _get_chat_records(client: Any, token: str, phone: str) -> str:
    resp = await _get(
        client,
        CHAT_RECORDS_URL,
        token=token,
        params={"phone": phone},
        step="chat_records",
        label="对话记录查询",
    )
    return resp.text


def _build_fill_prompt(
    medical_data: dict[str, Any],
    template_body: str,
    chat_body: str,
    query: str,
    date_time: str,
) -> str:
    return (
        MEDICAL_RECORD_FILL_PROMPT.replace("{{#1788251849452.data#}}", _to_text(medical_data))
        .replace("{{#17882507864850.body#}}", _to_text(template_body))
        .replace("{{#17893533679950.body#}}", _to_text(chat_body))
        .replace("{{#sys.query#}}", query)
        .replace("{{#1788254004426.dateTime#}}", date_time)
    )


async def run_medical_record(ctx: RunContext, query: str, model: Any) -> str:
    """执行病历填写完整链路，返回最终 result JSON 字符串。"""
    client = ConsultationClient(ctx)

    token = await _login(client, ctx.settings)

    patient_phone = _phone(ctx, "patientphone", "phone")
    if not patient_phone:
        raise DomainError(
            "patient_phone_missing", "缺少患者手机号，无法查询病历。", outcome="clarification"
        )

    medical_data = await _get_medical_info(client, token, patient_phone)

    user_role = await _get_user_role(client, token, patient_phone)
    user_id = user_role.get("UserId", "")
    if not user_id:
        raise DomainError(
            "user_id_missing", "未获取到用户编号，无法读取病历模板。", outcome="unavailable"
        )

    template_body = await _get_template_content(client, token, user_id)

    date_time = current_time()["dateTime"]

    # 对话记录是可选输入：手机号缺失或接口不可用都不阻断病历填写，告警后按空继续，
    # LLM 仍可依据病历信息 / 补充信息 / 医生修改 / 当前时间完成代写。
    request_phone = _phone(ctx, "requesterphone", "phone")
    chat_body = ""
    if not request_phone:
        logger.warning(
            "chat records skipped, request phone missing",
            extra={
                "run_id": ctx.record.run_id,
                "event_type": "chat_records_skipped_missing_phone",
            },
        )
    else:
        try:
            chat_body = await _get_chat_records(client, token, request_phone)
        except DomainError as exc:
            logger.warning(
                "chat records unavailable, continuing with empty conversation",
                extra={
                    "run_id": ctx.record.run_id,
                    "event_type": "chat_records_unavailable_continuing",
                    "fields": {"code": exc.code},
                },
            )

    prompt = _build_fill_prompt(medical_data, template_body, chat_body, query, date_time)

    await ctx.llm_budget.take()
    response = await model.ainvoke(
        prompt,
        config={
            "tags": ["consultation_room", "medical_record"],
            "metadata": {"component": "consultation_room_medical_record"},
        },
    )
    fill_text = _extract_content(response)

    result = parse_fill_result(medical_data, template_body, fill_text)

    # 保留中间产物到会话变量，便于后续分支复用。
    ctx.memory.set_variable("medical_info", medical_data)
    ctx.memory.set_variable("medical_user_role", user_role)
    ctx.memory.set_variable("medical_record_result", result["result"])

    return result["result"]
