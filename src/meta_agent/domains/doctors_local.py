"""
创建日期：2026-11-13
文件功能：在线医生查询本地实现（直连 webyun 在线医生列表，不经过 AI_WebApi）。

与 .NET ``DoctorToolsService.SearchDoctorsAsync`` 等价：
    POST 登录接口（固定服务账号）→ 缓存 Bearer token
    GET  在线医生列表 ?spaceId=<当前空间>
    → 投影成 ``search_doctors`` 的同构封套（numeric-envelope-v1）

因此上层 DoctorAdapter / evidence / facts / [DoctorRecommend:] 指令链完全不用改：

    {
      "status": 200,
      "message": "查询成功",
      "data": {
        "result": "一个医生" | "多个医生" | "无医生",
        "doctorNames": "张三、李四",
        "doctorIds": "5848,5849",
        "doctorPhones": "139...,138...",
        "command": "[DoctorRecommend:]139...",   # 仅 1 位医生且有手机号时
        "count": 2
      }
    }

与 .NET 的差异（故意收紧）：上游把鉴权失败放在 body.code（HTTP 仍是 200），
.NET 只看 HTTP 状态码，会把 401 当成「无医生」返回；这里抛
``DoctorUpstreamAuthError``，由调用方转成「暂不可用」，避免把鉴权故障说成
「当前没有在线医生」。
"""

import asyncio
import logging
import time
from typing import Any

import httpx

from meta_agent.config import Settings

logger = logging.getLogger(__name__)

# 直连 webyun 在线医生接口（可由 settings 覆盖）。
DEFAULT_LOGIN_URL = "http://www.ga-robot.com/webyun/api/Login/Login"
DEFAULT_QUERY_URL = "http://192.168.8.71:4000/api/users/all"
# 上游用这个文案表示「名单为空」，此时 data 为空数组或不存在。
EMPTY_MESSAGE = "暂无在线医生"
REQUEST_TIMEOUT = 15

# .NET 侧 ExtractToken 支持的路径（登录响应各版本字段位置不一致）。
_TOKEN_PATHS = (
    ("token",),
    ("data", "token"),
    ("data", "data", "token"),
    ("body", "token"),
    ("body", "data", "token"),
    ("body", "data", "data", "token"),
)


class DoctorUpstreamAuthError(RuntimeError):
    """上游鉴权失败（凭据错误 / token 失效），不代表「没有医生」。"""


# phone -> (token, expire_at_monotonic)
_TOKENS: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = asyncio.Lock()


def reset_token_cache() -> None:
    """清空 token 缓存（测试与配置热更新用）。"""
    _TOKENS.clear()


def _credentials(settings: Settings) -> tuple[str, str]:
    """医生专用账号优先，回退到场景/问诊共用的 webyun 服务账号。"""
    phone = (settings.doctor_login_phone or settings.webyun_login_phone or "").strip()
    password = settings.doctor_login_password.get_secret_value() or (
        settings.webyun_login_password.get_secret_value()
    )
    return phone, (password or "").strip()


def _extract_token(body: Any) -> str:
    """按 .NET 的顺序在登录响应里找 token。"""
    if not isinstance(body, dict):
        return ""
    for path in _TOKEN_PATHS:
        current: Any = body
        for segment in path:
            if not isinstance(current, dict) or segment not in current:
                current = None
                break
            current = current[segment]
        if isinstance(current, str) and current.strip():
            return current.strip()
    return ""


async def _login(client: httpx.AsyncClient, settings: Settings) -> str:
    phone, password = _credentials(settings)
    if not phone or not password:
        raise DoctorUpstreamAuthError("doctor login credentials are missing")
    url = (settings.doctor_backend_login_url or DEFAULT_LOGIN_URL).strip()
    response = await client.post(
        url,
        json={"Phone": phone, "Password": password},
        timeout=settings.doctor_backend_timeout_seconds,
    )
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("doctor login returned invalid json") from exc
    # 业务状态码在 body 里：非 200 视为鉴权/配置问题。
    code = body.get("code") if isinstance(body, dict) else None
    token = _extract_token(body)
    if not token:
        raise DoctorUpstreamAuthError(f"doctor login failed (code={code})")
    return token


async def _bearer(client: httpx.AsyncClient, settings: Settings) -> str:
    phone, _password = _credentials(settings)
    cached = _TOKENS.get(phone)
    now = time.monotonic()
    if cached and cached[1] > now:
        return cached[0]
    async with _TOKEN_LOCK:
        cached = _TOKENS.get(phone)
        now = time.monotonic()
        if cached and cached[1] > now:
            return cached[0]
        token = await _login(client, settings)
        _TOKENS[phone] = (token, now + float(settings.doctor_token_ttl_seconds))
        logger.info(
            "online doctor token refreshed",
            extra={"event_type": "http_call", "fields": {"step": "doctor_login"}},
        )
        return token


def _text(item: dict, key: str) -> str:
    value = item.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _project(body: Any) -> tuple[list[str], list[str], list[str]]:
    """把上游名单投影成 (names, ids, phones)，与 .NET 逐字段一致。"""
    if not isinstance(body, dict):
        raise RuntimeError("doctor list returned invalid json")
    raw_code = body.get("code")
    code: int | None = None
    if raw_code is not None:
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            code = None
    # 上游把鉴权失败也放在 body.code（HTTP 仍是 200），必须按失败处理。
    if code in {401, 403}:
        raise DoctorUpstreamAuthError(f"doctor list unauthorized (code={code})")
    if code is not None and code != 200:
        raise RuntimeError(f"doctor list upstream code {code}")
    message = body.get("message")
    data = body.get("data")
    if message == EMPTY_MESSAGE or not isinstance(data, list):
        return [], [], []
    names: list[str] = []
    ids: list[str] = []
    phones: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = _text(item, "name")
        if name:
            names.append(name)
        raw_id = item.get("id")
        if raw_id is not None:
            ids.append(raw_id if isinstance(raw_id, str) else str(raw_id))
        phone = _text(item, "phone")
        if phone:
            phones.append(phone)
    return names, ids, phones


def build_envelope(names: list[str], ids: list[str], phones: list[str]) -> dict[str, Any]:
    """生成 search_doctors 的同构封套（字段与 C# DTO 的 camelCase 序列化一致）。"""
    count = len(names)
    phones_text = ",".join(phones)
    if count == 1:
        result = "一个医生"
    elif count >= 2:
        result = "多个医生"
    else:
        result = "无医生"
    return {
        "status": 200,
        "message": "查询成功",
        "data": {
            "result": result,
            "doctorNames": "、".join(names),
            "doctorIds": ",".join(ids) if ids else "0",
            "doctorPhones": phones_text,
            "command": f"[DoctorRecommend:]{phones_text}" if count == 1 and phones_text else "",
            "count": count,
        },
    }


async def fetch_doctors(
    client: httpx.AsyncClient, settings: Settings, space_id: str
) -> tuple[list[str], list[str], list[str]]:
    """登录 + 查询在线医生名单（原始三元组）。"""
    token = await _bearer(client, settings)
    base = (settings.doctor_backend_query_url or DEFAULT_QUERY_URL).strip()
    separator = "&" if "?" in base else "?"
    url = f"{base}{separator}spaceId={space_id}"
    response = await client.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=settings.doctor_backend_timeout_seconds,
    )
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("doctor list returned invalid json") from exc
    return _project(body)


async def resolve_local_doctors(
    client: httpx.AsyncClient, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any]:
    """search_doctors 的本地实现：直连在线医生列表，返回同构封套。"""
    space_id = str(
        payload.get("spaceId")
        or settings.doctor_default_space_id
        or settings.scene_default_space_id
    )
    names, ids, phones = await fetch_doctors(client, settings, space_id)
    return build_envelope(names, ids, phones)
