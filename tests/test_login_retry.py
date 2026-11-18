"""
创建日期：2026-11-16
文件功能：验证 webyun 登录在「200 但不带 token」时会重试一次，而不是直接失败。
"""

import asyncio
from types import SimpleNamespace

import pytest

from meta_agent.contracts import DomainError
from meta_agent.flows.consultation_room.medical_record import _login


class _Resp:
    def __init__(self, body: dict) -> None:
        self._body = body
        self.status_code = 200
        self.text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


class _Client:
    """按顺序返回预设响应，并记录调用次数。"""

    def __init__(self, bodies: list[dict]) -> None:
        self._bodies = bodies
        self.calls = 0

    async def post(self, *args, **kwargs) -> _Resp:
        body = self._bodies[min(self.calls, len(self._bodies) - 1)]
        self.calls += 1
        return _Resp(body)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        webyun_login_phone="13800000000",
        webyun_login_password=SimpleNamespace(get_secret_value=lambda: "secret"),
    )


def test_login_retries_once_when_token_missing():
    client = _Client(
        [
            {"code": 200, "data": {"success": True}},  # 偶发：没有 token
            {"code": 200, "data": {"success": True, "token": "tok-2"}},
        ]
    )

    token = asyncio.run(_login(client, _settings()))

    assert token == "tok-2"
    assert client.calls == 2


def test_login_fails_after_two_empty_responses():
    client = _Client([{"code": 200, "data": {"success": False}}])

    with pytest.raises(DomainError) as exc:
        asyncio.run(_login(client, _settings()))

    assert exc.value.code == "medical_record_token_missing"
    assert client.calls == 2
