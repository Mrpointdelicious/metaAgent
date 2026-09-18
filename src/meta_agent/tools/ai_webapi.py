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
    }
)
REHAB_ENDPOINTS = frozenset({"navigate_scene", "search_doctors"})


class BackendCallError(DomainError):
    pass


class AIWebApiClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.patient_base = settings.ai_webapi_base_url.rstrip("/")
        origin = urlsplit(self.patient_base)
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

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.ai_webapi_bearer_token:
            headers["Authorization"] = f"Bearer {self.settings.ai_webapi_bearer_token}"
        return headers

    async def post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if endpoint not in PATIENT_ENDPOINTS | REHAB_ENDPOINTS:
            raise BackendCallError("unknown_tool", "未注册的工具端点。")
        if self.settings.dry_run:
            from meta_agent.tools.demo import demo_response

            return demo_response(endpoint, payload)
        base = self.patient_base if endpoint in PATIENT_ENDPOINTS else self.rehab_base
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
