"""
创建日期：2026-11-16
文件功能：线下处方的排班可用时段计算（对应 Dify「单疾病输出 / 多疾病输出」里的
一组时间工具函数）。

排班结构来自处方详情 ScheduleInfo：
    {TenantName: 医院名, Schedule: {"2026/11/16": [{"Time": "08:00-09:00", "Used": 1}, ...]}}
Used = 0 表示空闲；缺的时段用默认 10 个补全。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

DEFAULT_TIME_SLOTS: tuple[dict[str, Any], ...] = (
    {"Time": "08:00-09:00", "Used": 0},
    {"Time": "09:00-10:00", "Used": 0},
    {"Time": "10:00-11:00", "Used": 0},
    {"Time": "11:00-12:00", "Used": 0},
    {"Time": "12:00-13:00", "Used": 0},
    {"Time": "13:00-14:00", "Used": 0},
    {"Time": "14:00-15:00", "Used": 0},
    {"Time": "15:00-16:00", "Used": 0},
    {"Time": "16:00-17:00", "Used": 0},
    {"Time": "17:00-18:00", "Used": 0},
)

TIME_PERIOD_MAPPING: dict[str, tuple[str, ...]] = {
    "上午": ("08:00-09:00", "09:00-10:00", "10:00-11:00"),
    "中午": ("11:00-12:00", "12:00-13:00"),
    "下午": ("13:00-14:00", "14:00-15:00", "15:00-16:00", "16:00-17:00", "17:00-18:00"),
    "晚上": ("17:00-18:00",),
}

# 无空位兜底时给的时间段。
_FALLBACK_TIME = {"上午": "09:00-10:00", "中午": "12:00-13:00", "下午": "14:00-15:00"}
_FALLBACK_DEFAULT = "14:00-15:00"


def complete_schedule(schedule: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    """把每天缺的默认时段补上，保证可用性判断不会被「只返回已占用时段」骗到。"""
    if not schedule:
        return {}
    completed: dict[str, list[dict[str, Any]]] = {}
    for date_str, slots in schedule.items():
        if not slots:
            completed[date_str] = [dict(slot) for slot in DEFAULT_TIME_SLOTS]
            continue
        existing = {slot.get("Time", "") for slot in slots if slot.get("Time", "")}
        merged = [dict(slot) for slot in slots]
        for default_slot in DEFAULT_TIME_SLOTS:
            if default_slot["Time"] not in existing:
                merged.append(dict(default_slot))
        completed[date_str] = merged
    return completed


def extract_occupied_slots(schedule: dict[str, Any] | None) -> list[str]:
    """已占用时段，格式 "YYYY/MM/DD HH:MM-HH:MM"。"""
    occupied: list[str] = []
    for date_str, slots in complete_schedule(schedule).items():
        for slot in slots:
            try:
                if int(slot.get("Used", 0)) != 0:
                    occupied.append(f"{date_str} {slot.get('Time', '')}".strip())
            except (TypeError, ValueError):
                continue
    return occupied


def normalize_date(date_str: str | None) -> str:
    """各种日期写法统一成 YYYY/MM/DD；识别不了就只把 "-" 换成 "/"。"""
    if not date_str:
        return ""
    text = date_str.split()[0] if " " in date_str else date_str
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y年%m月%d日", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y/%m/%d")
        except ValueError:
            continue
    try:
        dt = datetime.strptime(f"{datetime.now().year}年{text}", "%Y年%m月%d日")
        return dt.strftime("%Y/%m/%d")
    except ValueError:
        return text.replace("-", "/")


def calculate_relative_date(relative_term: str, base_date: datetime | None = None) -> str | None:
    """「明天 / 下周 / 下月」这类词 → 具体日期；不认识返回 None。"""
    base = base_date or datetime.now()
    terms: dict[str, timedelta] = {
        "今天": timedelta(days=0),
        "明天": timedelta(days=1),
        "后天": timedelta(days=2),
        "大后天": timedelta(days=3),
        "本周": -timedelta(days=base.weekday()),
        "下周": timedelta(days=7 - base.weekday()),
        "下下周": timedelta(days=14 - base.weekday()),
    }
    if relative_term in terms:
        return (base + terms[relative_term]).strftime("%Y/%m/%d")
    if relative_term in ("本月", "下月", "下下月"):
        first = base.replace(day=1)
        months = {"本月": 0, "下月": 1, "下下月": 2}[relative_term]
        for _ in range(months):
            first = (first + timedelta(days=32)).replace(day=1)
        return first.strftime("%Y/%m/%d")
    return None


def available_slots(
    schedule: dict[str, Any] | None,
    target_date: str | None = None,
    time_period: str | None = None,
) -> list[str]:
    """按日期 / 时段筛出空闲时段，返回 "YYYY/MM/DD HH:MM-HH:MM" 列表。"""
    if not schedule:
        return []
    completed = complete_schedule(schedule)

    wanted_date = ""
    if target_date:
        wanted_date = calculate_relative_date(target_date) or normalize_date(target_date)
    wanted_ranges = TIME_PERIOD_MAPPING.get(time_period or "", ())

    found: list[str] = []
    for date_str, slots in completed.items():
        if wanted_date and date_str != wanted_date:
            continue
        for slot in slots:
            time_text = str(slot.get("Time", "")).strip()
            try:
                used = int(slot.get("Used", 0))
            except (TypeError, ValueError):
                used = 0
            if used != 0:
                continue
            if wanted_ranges and time_text not in wanted_ranges:
                continue
            found.append(f"{date_str} {time_text}".strip())
    return found


def pick_slot(
    schedule: dict[str, Any] | None,
    target_date: str | None = None,
    time_period: str | None = None,
    rng: random.Random | None = None,
) -> str:
    """随机取一个「还没结束」的空闲时段；一个都没有就按目标日期兜底给一个。"""
    chooser = rng or random
    now = datetime.now()

    future: list[str] = []
    for slot in available_slots(schedule, target_date, time_period):
        try:
            date_part, time_range = slot.split()
            end_time = time_range.split("-")[1]
            if datetime.strptime(f"{date_part} {end_time}", "%Y/%m/%d %H:%M") > now:
                future.append(slot)
        except (IndexError, ValueError):
            continue
    if future:
        return chooser.choice(future)

    if target_date:
        try:
            date_obj = datetime.strptime(normalize_date(target_date), "%Y/%m/%d")
        except ValueError:
            date_obj = now
    else:
        date_obj = now
    if date_obj.date() == now.date():
        date_obj = now + timedelta(days=1)

    fallback = _FALLBACK_TIME.get(time_period or "", _FALLBACK_DEFAULT)
    return f"{date_obj.strftime('%Y/%m/%d')} {fallback}"


def parse_date_input(date_input: str | None) -> tuple[str | None, str | None]:
    """把「2026/11/17 下午」拆成 (日期, 时段)。"""
    if not date_input or date_input == "null":
        return None, None
    parts = date_input.strip().split()
    date_part = parts[0] if parts else None
    period = parts[1] if len(parts) >= 2 else None
    return date_part, period
