"""
创建日期：2026-10-23
文件功能：验证场景指令前的确定性「指令分流」与量表→评估室映射。
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from meta_agent.domains.instruction_classify import (
    classify_instruction,
    extract_time_range,
    scale_to_room,
)


def test_classify_instruction():
    assert classify_instruction("做量表") == "0"
    assert classify_instruction("想训练") == "0"
    assert classify_instruction("做AD8量表") == "1"
    assert classify_instruction("做记忆训练") == "1"
    assert classify_instruction("这个月") == "2"
    assert classify_instruction("修改病历") == "2"
    assert classify_instruction("去二楼") == "3"


def test_extract_time_range():
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    fmt = "%Y/%m/%d"

    # 昨天 → (昨天, 今天)
    start, end = extract_time_range("昨天")
    assert start == (today - timedelta(days=1)).strftime(fmt)
    assert end == today.strftime(fmt)

    # 这个月 → (30天前, 明天)
    start, end = extract_time_range("这个月")
    assert start == (today - timedelta(days=30)).strftime(fmt)
    assert end == (today + timedelta(days=1)).strftime(fmt)

    # 8月1号到13号 → (今年8/1, 今年8/14)（结束日 +1 的开区间）
    start, end = extract_time_range("8月1号到13号")
    assert start == f"{today.year}/08/01"
    assert end == f"{today.year}/08/14"


def test_scale_to_room():
    assert scale_to_room("AD8") == "四楼1号评估室"
    assert scale_to_room("MoCA") == "四楼6号评估室"
    assert scale_to_room("巴氏指数") == "四楼2号评估室"
    assert scale_to_room("记忆训练") == "未知评估室"


def test_is_command_query_for_scale_intent():
    from meta_agent.domains.scene_instruction import is_command_query

    assert is_command_query("做AD8量表") is True
    assert is_command_query("做量表") is True
    assert is_command_query("想训练") is True
    assert is_command_query("去二楼") is True
    assert is_command_query("AD8是什么") is False  # 提问不是指令
    assert is_command_query("训练报告") is False  # 查报告不是场景指令


def test_is_command_query_for_record_query():
    from meta_agent.domains.scene_instruction import is_command_query

    assert is_command_query("查这个月的报告") is True  # 含时间 → result 2
    assert is_command_query("修改病历") is True  # 修改病历 → result 2


def test_extract_token():
    from meta_agent.domains.scene_instruction import _extract_token

    assert _extract_token({"token": "abc"}) == "abc"
    assert _extract_token({"data": {"token": "abc"}}) == "abc"
    assert _extract_token({"data": {"data": {"token": "abc"}}}) == "abc"
    assert _extract_token({"body": '{"data": {"token": "abc"}}'}) == "abc"
    assert _extract_token({"foo": "bar"}) == ""


def test_latest_prescribed_scale():
    async def check():
        from meta_agent.domains import scene_instruction as si

        class FakeResp:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class FakeClient:
            def __init__(self, responses):
                self._responses = list(responses)

            async def get(self, url, params=None, timeout=None):
                return FakeResp(self._responses.pop(0))

        today = datetime.now()
        old = (today - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        now = today.strftime("%Y-%m-%d %H:%M:%S")
        records = {"data": [{"Id": 1, "CreateTime": old}, {"Id": 42, "CreateTime": now}]}
        order = {"data": {"PrescriptionInfo": [{"ScaleName": "MoCA"}]}}
        client = FakeClient([records, order])
        ctx = SimpleNamespace(
            record=SimpleNamespace(raw_inputs={"requesterphone": "15900000000"}),
            backend=SimpleNamespace(client=client),
        )
        scale = await si._latest_prescribed_scale(ctx)
        assert scale == "MoCA"

    asyncio.run(check())


def test_latest_prescribed_scale_no_records():
    async def check():
        from meta_agent.domains import scene_instruction as si

        class FakeResp:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class FakeClient:
            async def get(self, url, params=None, timeout=None):
                return FakeResp({"data": []})

        ctx = SimpleNamespace(
            record=SimpleNamespace(raw_inputs={"requesterphone": "15900000000"}),
            backend=SimpleNamespace(client=FakeClient()),
        )
        assert await si._latest_prescribed_scale(ctx) is None

    asyncio.run(check())
