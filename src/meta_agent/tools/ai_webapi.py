"""
创建日期：2026-08-29
文件功能：以异步、限时和服务鉴权方式调用现有 AI_WebApi 康复工具接口。
"""

from typing import Any

import httpx

from meta_agent.config import Settings


class BackendCallError(RuntimeError):
    """表示下游工具调用失败，但不携带敏感响应正文。"""


class AIWebApiClient:
    """AI_WebApi 的强边界客户端。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.ai_webapi_base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(settings.ai_webapi_timeout_seconds),
        )

    async def close(self) -> None:
        """释放连接池。"""
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._settings.ai_webapi_bearer_token:
            headers["Authorization"] = (
                f"Bearer {self._settings.ai_webapi_bearer_token}"
            )
        return headers

    async def post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """调用一个工具接口并只返回 JSON 对象。"""
        try:
            response = await self._client.post(
                endpoint.lstrip("/"),
                json=payload,
                headers=self._headers(),
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BackendCallError("AI_WebApi 调用失败") from exc
        if not isinstance(body, dict):
            raise BackendCallError("AI_WebApi 返回结构不是 JSON 对象")
        return body

    async def ping(self) -> tuple[bool, str]:
        """检查 OpenAPI 端点可达性，不执行患者查询。"""
        if self._settings.dry_run:
            return True, "dry_run"
        try:
            response = await self._client.get("openapi.json", headers=self._headers())
            response.raise_for_status()
            return True, "reachable"
        except httpx.HTTPError:
            return False, "unreachable"
