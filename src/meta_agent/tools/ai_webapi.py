"""
创建日期：2026-09-08
文件功能：仅调用静态.NET工具端点，限制响应体、超时和制品来源。
"""

import json
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from meta_agent.config import Settings
from meta_agent.contracts import DomainError
from meta_agent.orchestration.identity import TrustedScope

logger = logging.getLogger(__name__)

PATIENT_ENDPOINTS = frozenset(
    {
        "get_multisource_patient_context",
        "get_irego_session_analysis",
        "get_irego_patient_history",
        "get_irego_longitudinal_analysis",
        "generate_irego_single_session_report",
        "generate_irego_longitudinal_report",
        "get_iretour_patient_context",
        "get_iretour_patient_history",
        "get_iretour_session_analysis",
        "get_iretour_longitudinal_analysis",
        "generate_iretour_single_session_report",
        "generate_iretour_longitudinal_report",
        "get_irecog_patient_context",
        "get_irecog_patient_history",
        "get_irecog_session_analysis",
        "get_irecog_longitudinal_analysis",
        "generate_irecog_single_session_report",
        "generate_irecog_longitudinal_report",
        "get_assessment_patient_context",
        "get_assessment_patient_history",
        "get_assessment_result_analysis",
        "get_assessment_longitudinal_analysis",
        "generate_assessment_single_report",
        "generate_assessment_longitudinal_report",
        "resolve_patient_identity",
        "get_patient_profile",
        "get_patient_consultation",
        "get_patient_rehab",
    }
)
REHAB_ENDPOINTS = frozenset({"navigate_scene", "search_doctors"})
HOSPITAL_ENDPOINTS = frozenset({"query_hospital_operations"})
IRETOUR_REPORT_ENDPOINTS = frozenset(
    {"generate_iretour_single_session_report", "generate_iretour_longitudinal_report"}
)
IRECOG_REPORT_ENDPOINTS = frozenset(
    {"generate_irecog_single_session_report", "generate_irecog_longitudinal_report"}
)
ASSESSMENT_REPORT_ENDPOINTS = frozenset(
    {"generate_assessment_single_report", "generate_assessment_longitudinal_report"}
)
# 三层患者接口使用 project 患者编号，独立于已停用的多源端点。
PATIENT_ENDPOINTS_V17 = frozenset(
    {
        "get_patient_profile",
        "get_patient_consultation",
        "get_patient_rehab",
    }
)


class BackendCallError(DomainError):
    pass


async def _read_error_body(response) -> str | None:
    """非 2xx 时读取受限响应体，尽量取后端给出的 patient_message/message。"""
    try:
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > 65536:
                break
        if not content:
            return None
        body = json.loads(content)
        if isinstance(body, dict):
            for key in ("patient_message", "message", "detail"):
                value = body.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    except Exception:
        return None
    return None


def _http_fallback_message(code: int) -> str:
    """非 2xx 且后端未返回 patient_message 时的中性兜底文案，不向用户暴露 HTTP 状态码。"""
    if code == 404:
        return "未查询到相关记录。"
    if code in {408, 429, 502, 503, 504}:
        return "服务暂时繁忙，请稍后再试。"
    return "该项数据暂不可用。"


def normalize_artifact_url(settings: Settings, url: str) -> str:
    """把后端返回的制品 URL 归一化到公开反代前缀。

    后端拼 image_url 时用的是内部路径 /api/ai/...，公开入口是配置里
    AI_WEBAPI_*_BASE_URL 的 /aiwebapi/ai/...。settings.ai_webapi_artifact_path_prefix
    非空时，把首段 /api/ 换成该前缀（/api/ai/... -> /aiwebapi/ai/...）；留空则不重写。
    """
    prefix = (getattr(settings, "ai_webapi_artifact_path_prefix", "") or "").strip().strip("/")
    if not prefix or not isinstance(url, str):
        return url
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or parts.username or parts.password:
        return url
    path = parts.path
    if not path.startswith("/api/"):
        return url
    path = "/" + prefix + "/" + path[len("/api/") :]
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


class AIWebApiClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.patient_base = settings.ai_webapi_base_url.rstrip("/")
        origin = urlsplit(self.patient_base)
        self.hospital_base = settings.ai_webapi_hospital_base_url.rstrip("/") or urlunsplit(
            (origin.scheme, origin.netloc, "/api/ai/hospital-operations/tools", "", "")
        )
        # 机构目录是 hospital-operations 域的独立 GET 接口（/tools 的兄弟路径 /directory）。
        self.hospital_directory_url = self.hospital_base.rsplit("/", 1)[0] + "/directory"
        self.rehab_base = settings.ai_webapi_rehab_base_url.rstrip("/") or urlunsplit(
            (origin.scheme, origin.netloc, "/api/ai/rehab/tools", "", "")
        )
        # swagger 仅在后端开发环境启用；工具契约文档在所有环境可用。
        self.health_url = settings.ai_webapi_health_url or urlunsplit(
            (origin.scheme, origin.netloc, "/api/ai/patients/tools/openapi.json", "", "")
        )
        self.client = httpx.AsyncClient(
            timeout=settings.ai_webapi_timeout_seconds,
            transport=transport,
            follow_redirects=False,
            limits=httpx.Limits(
                max_connections=settings.max_concurrent_tools,
                max_keepalive_connections=settings.max_concurrent_tools,
            ),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def resolve_patient_identity(self, scope: TrustedScope) -> TrustedScope:
        body = await self.post("resolve_patient_identity", scope.patient_payload({}))
        return scope.bind(body)

    def headers(self, endpoint: str | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token = self.settings.ai_webapi_bearer_token
        if endpoint in HOSPITAL_ENDPOINTS and self.settings.ai_webapi_hospital_bearer_token:
            token = self.settings.ai_webapi_hospital_bearer_token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if endpoint not in (
            PATIENT_ENDPOINTS | REHAB_ENDPOINTS | PATIENT_ENDPOINTS_V17 | HOSPITAL_ENDPOINTS
        ):
            raise BackendCallError("unknown_tool", "未注册的工具端点。")
        if (
            endpoint == "get_multisource_patient_context"
            and not self.settings.multisource_patient_context_enabled
        ):
            raise BackendCallError(
                "capability_disabled", "多源患者上下文暂未启用。", outcome="unsupported"
            )
        if endpoint in IRETOUR_REPORT_ENDPOINTS and not self.settings.iretour_reports_enabled:
            raise BackendCallError(
                "capability_disabled",
                "IReTour报表暂未启用，仍可查询历史和分析结果。",
                outcome="unsupported",
            )
        if endpoint in IRECOG_REPORT_ENDPOINTS and not self.settings.irecog_reports_enabled:
            raise BackendCallError(
                "capability_disabled",
                "IReCog报表暂未启用，仍可查询历史和分析结果。",
                outcome="unsupported",
            )
        if endpoint in ASSESSMENT_REPORT_ENDPOINTS and not self.settings.assessment_reports_enabled:
            raise BackendCallError(
                "capability_disabled",
                "量表报表暂未启用，仍可查询历史和分析结果。",
                outcome="unsupported",
            )
        if self.settings.dry_run:
            from meta_agent.tools.demo import demo_response

            return demo_response(endpoint, payload)
        # 场景指令由 agent 本地解析：直连点位库 + 本地匹配，不再请求 .NET 端点。
        # 仍返回 commands-array-v1 同构封套，上层的 evidence/action_ref 校验链不变。
        if endpoint == "navigate_scene" and self.settings.scene_local_enabled:
            from meta_agent.domains.scene_local import resolve_local_scene

            try:
                return await resolve_local_scene(self.client, self.settings, payload)
            except httpx.HTTPError as exc:
                raise BackendCallError(
                    "transport_error", "场景点位库暂时无法访问。", retryable=True
                ) from exc
            except (ValueError, TypeError, RuntimeError) as exc:
                raise BackendCallError("invalid_contract", "场景点位库返回了无效数据。") from exc
        # 在线医生同样本地查询：登录 webyun 拿 token + 拉在线名单，不请求 .NET 端点。
        # 仍返回 numeric-envelope 同构封套，上层 DoctorAdapter / evidence 链不变。
        if endpoint == "search_doctors" and self.settings.doctors_local_enabled:
            from meta_agent.domains.doctors_local import (
                DoctorUpstreamAuthError,
                resolve_local_doctors,
            )

            try:
                return await resolve_local_doctors(self.client, self.settings, payload)
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                raise BackendCallError(
                    f"http_{code}",
                    "在线医生服务暂时不可用。",
                    retryable=code in {408, 429, 502, 503, 504},
                    outcome="unavailable" if code in {401, 403} else "failed",
                ) from exc
            except DoctorUpstreamAuthError as exc:
                raise BackendCallError(
                    "doctor_upstream_unauthorized",
                    "在线医生服务暂时不可用。",
                    outcome="unavailable",
                ) from exc
            except httpx.HTTPError as exc:
                raise BackendCallError(
                    "transport_error", "在线医生服务暂时无法访问。", retryable=True
                ) from exc
            except (ValueError, TypeError, RuntimeError) as exc:
                raise BackendCallError("invalid_contract", "在线医生服务返回了无效数据。") from exc
        base = (
            self.hospital_base
            if endpoint in HOSPITAL_ENDPOINTS
            else self.rehab_base
            if endpoint in REHAB_ENDPOINTS
            else self.patient_base
        )
        try:
            async with self.client.stream(
                "POST", f"{base}/{endpoint}", json=payload, headers=self.headers(endpoint)
            ) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self.settings.max_tool_response_bytes:
                        raise BackendCallError(
                            "response_too_large", "工具结果过大，请缩小查询范围。"
                        )
            if not response.is_success:
                code = response.status_code
                reason = None
                try:
                    error = json.loads(content)
                    reasons = error.get("reason_codes") if isinstance(error, dict) else None
                    if isinstance(reasons, list) and reasons and isinstance(reasons[0], str):
                        reason = reasons[0]
                except (ValueError, UnicodeDecodeError):
                    pass
                logger.warning(
                    "backend tool error",
                    extra={
                        "event_type": "backend_tool_error",
                        "dependency": "ai_webapi",
                        "fields": {
                            "endpoint": endpoint,
                            "http_status": code,
                            "reason": reason,
                            "base": base,
                        },
                    },
                )
                raise BackendCallError(
                    reason or f"http_{code}",
                    "患者登录凭证已失效或权限不足，请重新登录。"
                    if code in {401, 403}
                    else f"工具服务返回错误（HTTP {code}）。",
                    retryable=code in {408, 429, 502, 503, 504},
                    outcome="unavailable" if code in {401, 403} else "failed",
                )
            body = json.loads(content)
        except httpx.RequestError as exc:
            raise BackendCallError(
                "transport_error", "工具服务暂时无法访问。", retryable=True
            ) from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise BackendCallError("invalid_json", "工具服务返回了无效数据。") from exc
        if not isinstance(body, dict):
            raise BackendCallError("invalid_envelope", "工具返回的封套类型不正确。")
        return body

    async def directory(self) -> list[dict]:
        """拉取医院运营机构目录，只投影 hospital_id + hospital_name。"""
        if self.settings.dry_run:
            from meta_agent.tools.demo import demo_directory

            return demo_directory()
        token = (
            self.settings.ai_webapi_hospital_bearer_token or self.settings.ai_webapi_bearer_token
        )
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with self.client.stream(
                "GET", self.hospital_directory_url, headers=headers
            ) as response:
                if not response.is_success:
                    code = response.status_code
                    logger.warning(
                        "hospital directory error",
                        extra={
                            "event_type": "backend_tool_error",
                            "dependency": "ai_webapi",
                            "fields": {
                                "endpoint": "directory",
                                "http_status": code,
                                "url": self.hospital_directory_url,
                            },
                        },
                    )
                    raise BackendCallError(
                        f"http_{code}",
                        "机构目录暂时无法获取。",
                        retryable=code in {408, 429, 502, 503, 504},
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self.settings.max_tool_response_bytes:
                        raise BackendCallError("response_too_large", "机构目录过大，无法处理。")
            body = json.loads(content)
        except httpx.RequestError as exc:
            raise BackendCallError(
                "transport_error", "机构目录服务暂时无法访问。", retryable=True
            ) from exc
        if not isinstance(body, list):
            raise BackendCallError("invalid_contract", "机构目录返回格式异常。")
        directory: list[dict] = []
        for item in body:
            if (
                not isinstance(item, dict)
                or type(item.get("hospital_id")) is not int
                or item["hospital_id"] <= 0
                or not isinstance(item.get("hospital_name"), str)
                or not item["hospital_name"].strip()
            ):
                raise BackendCallError("invalid_contract", "机构目录包含无效条目。")
            directory.append(
                {"hospital_id": item["hospital_id"], "hospital_name": item["hospital_name"].strip()}
            )
        return directory

    async def artifact_available(self, url: str) -> bool:
        url = normalize_artifact_url(self.settings, url)
        parts = urlsplit(url)
        # 制品可能来自任一已配置后端（患者 / 康复 / 医院运营），不只 patient_base。
        allowed = {
            urlsplit(self.patient_base).hostname,
            urlsplit(self.rehab_base).hostname,
            urlsplit(self.hospital_base).hostname,
            *self.settings.artifact_allowed_hosts,
        }
        if (
            parts.scheme not in {"http", "https"}
            or parts.hostname not in allowed
            or parts.username
            or parts.password
        ):
            return False
        try:
            async with self.client.stream("HEAD", url) as response:
                if response.status_code != 405:
                    return response.is_success and response.headers.get(
                        "content-type", ""
                    ).startswith("image/")
            async with self.client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
                return response.is_success and response.headers.get("content-type", "").startswith(
                    "image/"
                )
        except httpx.HTTPError:
            return False

    async def ping(self) -> tuple[bool, str]:
        if self.settings.dry_run:
            return True, "dry_run"
        try:
            response = await self.client.get(self.health_url, headers=self.headers())
            return (True, "reachable") if response.is_success else (False, "unreachable")
        except httpx.HTTPError:
            return False, "unreachable"
