"""
创建日期：2026-09-08
文件功能：仅调用静态.NET工具端点，限制响应体、超时和制品来源。
"""

import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from meta_agent.config import Settings
from meta_agent.contracts import DomainError

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

    async def resolve_patient_identity(self, *, project_patient_id=None, phone=None) -> dict:
        """参数只允许来自认证网关 inputs；此接口不暴露给语言模型。"""
        if (project_patient_id is None) == (phone is None):
            raise BackendCallError("invalid_identity", "请提供且仅提供一种可信患者标识。")
        if project_patient_id is not None:
            value = str(project_patient_id)
            if not value.isascii() or not value.isdigit() or int(value) <= 0:
                raise BackendCallError("invalid_identity", "项目患者编号须为正整数。")
            payload = {"project_patient_id": int(value)}
        else:
            if not isinstance(phone, str) or not phone.strip() or len(phone) > 32:
                raise BackendCallError("invalid_identity", "可信手机号无效。")
            payload = {"phone": phone.strip()}
        body = await self.post("resolve_patient_identity", payload)
        data = body.get("data")
        if (
            body.get("tool_name") != "resolve_patient_identity"
            or body.get("contract_version") != "1.6.0"
            or body.get("status") != "success"
            or not isinstance(data, dict)
            or data.get("binding_status") != "resolved"
            or type(data.get("robot_patient_id")) is not int
            or data["robot_patient_id"] <= 0
            or type(data.get("project_patient_id")) is not int
            or data["project_patient_id"] <= 0
            or (
                project_patient_id is not None
                and data.get("project_patient_id") != int(project_patient_id)
            )
        ):
            raise BackendCallError(
                "identity_unresolved", "无法唯一确认患者身份。", outcome="clarification"
            )
        return data

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.ai_webapi_bearer_token:
            headers["Authorization"] = f"Bearer {self.settings.ai_webapi_bearer_token}"
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
        if self.settings.dry_run:
            from meta_agent.tools.demo import demo_response

            return demo_response(endpoint, payload)
        base = (
            self.hospital_base
            if endpoint in HOSPITAL_ENDPOINTS
            else self.rehab_base
            if endpoint in REHAB_ENDPOINTS
            else self.patient_base
        )
        try:
            async with self.client.stream(
                "POST", f"{base}/{endpoint}", json=payload, headers=self.headers()
            ) as response:
                if not response.is_success:
                    code = response.status_code
                    raise BackendCallError(
                        f"http_{code}",
                        f"工具服务返回错误（HTTP {code}）。",
                        retryable=code in {408, 429, 502, 503, 504},
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self.settings.max_tool_response_bytes:
                        raise BackendCallError(
                            "response_too_large", "工具结果过大，请缩小查询范围。"
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

    async def artifact_available(self, url: str) -> bool:
        parts = urlsplit(url)
        allowed = {urlsplit(self.patient_base).hostname, *self.settings.artifact_allowed_hosts}
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
