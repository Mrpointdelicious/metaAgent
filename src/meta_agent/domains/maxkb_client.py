"""
创建日期：2026-10-18
文件功能：直连固定版本 MaxKB 管理会话 API，限制回包并处理会话过期。
"""

import asyncio
import json
import time
from urllib.parse import urlsplit

from fastapi import HTTPException


def validate_url(value, *, path=None):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (path is not None and parsed.path.rstrip("/") != path)
    ):
        raise RuntimeError("Invalid MaxKB service/provider URL")
    return value.rstrip("/")


async def json_request(http, method, url, *, limit=8_000_000, **kwargs):
    # Never propagate provider response text: it may contain credentials.
    async with http.stream(method, url, **kwargs) as response:
        raw = bytearray()
        async for part in response.aiter_bytes():
            raw.extend(part)
            if len(raw) > limit:
                raise HTTPException(502, "maxkb_response_too_large")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(502, "maxkb_invalid_response") from None
        if not isinstance(body, dict):
            raise HTTPException(502, "maxkb_invalid_response")
        return response.status_code, body


class MaxKBClient:
    def __init__(self, http, base_url, username, password):
        self.http = http
        self.base = validate_url(base_url, path="/admin/api")
        self.username, self.password = username, password
        self.token, self.token_at = "", 0.0
        self.lock = asyncio.Lock()

    async def login(self):
        async with self.lock:
            if self.token and time.monotonic() - self.token_at < 600:
                return
            status, body = await json_request(
                self.http,
                "POST",
                self.base + "/user/login",
                limit=65536,
                json={"username": self.username, "password": self.password},
            )
            data = body.get("data")
            if status != 200 or body.get("code") not in (0, 200) or not isinstance(data, dict):
                raise HTTPException(503, "maxkb_login_rejected")
            token = data.get("token")
            if not isinstance(token, str) or not token:
                raise HTTPException(503, "maxkb_login_rejected")
            self.token, self.token_at = token, time.monotonic()

    async def call(self, method, path, **kwargs):
        for attempt in range(2):
            await self.login()
            used_token = self.token
            status, body = await json_request(
                self.http,
                method,
                self.base + path,
                headers={"Authorization": "Bearer " + used_token},
                **kwargs,
            )
            if status == 401 or body.get("code") == 401:
                if self.token == used_token:
                    self.token = ""
                if attempt == 0:
                    continue
                raise HTTPException(503, "maxkb_authentication_expired")
            if status == 404 or body.get("code") == 404:
                raise HTTPException(404, "maxkb_resource_not_found")
            if status != 200 or body.get("code") not in (0, 200):
                raise HTTPException(503, "maxkb_request_failed")
            return body.get("data")
        raise HTTPException(503, "maxkb_authentication_expired")
