"""
创建日期：2026-10-23
文件功能：验证场景指令前的确定性「指令分流」与量表→评估室映射。
"""

from datetime import datetime, timedelta

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
