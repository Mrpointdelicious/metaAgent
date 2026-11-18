"""
创建日期：2026-11-16
文件功能：把「处方列表 + 处方详情」拼成前端可直接渲染的推荐结果，并生成 summary。
        对应 Dify 的「单疾病输出 / 多疾病输出」两个代码节点里与排班无关的部分。

输出契约（answer 就是这段 JSON 文本）：
    {"online_prescriptions": [...], "offline_prescriptions": [...], "summary": "..."}
"""

from __future__ import annotations

import random
import re
from typing import Any

from .slots import extract_occupied_slots, parse_date_input, pick_slot

# 单疾病：线上 6 个、线下 8 个、总数 14 个；多疾病按疾病各 6/8，合并后同样收口到 6/8/14。
MAX_ONLINE = 6
MAX_OFFLINE = 8
MAX_TOTAL = 14

EMPTY_RESULT: dict[str, Any] = {
    "online_prescriptions": [],
    "offline_prescriptions": [],
    "summary": "",
}

# 疾病名粗匹配（原节点硬编码的三个词表）。
DISEASE_KEYWORDS = {
    "脑卒中": ("脑卒中", "卒中", "中风", "脑血管"),
    "骨质疏松症": ("骨质疏松", "骨质", "骨骼"),
    "肌少症": ("肌少症", "肌肉减少", "肌无力"),
}


def _unique(items: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    out: list[Any] = []
    for item in items:
        key = item.get("Id") if isinstance(item, dict) else item
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def combine(items: list[dict[str, Any]], details: dict[str, dict[str, Any]]) -> list[tuple]:
    """把列表项与详情配对；详情缺失就跳过（等价原节点的 continue）。"""
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in items:
        detail = details.get(str(item.get("Id")))
        if detail:
            pairs.append((item, detail))
    return pairs


def split_online_offline(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> tuple[list[tuple], list[tuple]]:
    """Type=1 是线下，其余（含未知）都按线上处理。"""
    online: list[tuple] = []
    offline: list[tuple] = []
    for pair in pairs:
        type_code = pair[1].get("Type")
        (offline if type_code == 1 else online).append(pair)
    return online, offline


def recommend_online(pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    """线上处方：EvaluationList 里的量表名，空时兜底「AD8测评量表」。"""
    result: list[dict[str, Any]] = []
    for item, detail in pairs:
        evaluations: list[str] = []
        for entry in detail.get("EvaluationList") or []:
            if isinstance(entry, str):
                evaluations.append(entry)
            elif isinstance(entry, dict) and entry.get("GameName"):
                evaluations.append(str(entry["GameName"]))
        if not evaluations:
            evaluations = ["AD8测评量表"]
        entry: dict[str, Any] = {
            "Id": detail.get("Id", item.get("Id")),
            "Name": detail.get("Name") or item.get("Name") or f"处方{detail.get('Id')}",
            "Indication": detail.get("Indication", ""),
            "Prescription": detail.get("Prescription", ""),
            "Details": evaluations,
        }
        if detail.get("Type") is not None:
            entry["Type"] = detail["Type"]
        result.append(entry)
    return result


def _split_location(location: str | None) -> list[str]:
    if not location or location == "null":
        return []
    return [part.strip() for part in re.split(r"[\n、,，]+", location) if part.strip()]


def _schedule_of(item: dict[str, Any], detail: dict[str, Any]) -> Any:
    if item.get("ScheduleInfo") is not None:
        return item.get("ScheduleInfo")
    return detail.get("ScheduleInfo", [])


def _choose_hospitals(
    schedule_info: list[dict[str, Any]],
    location: str | None,
    rng: random.Random | None,
) -> tuple[list[dict[str, Any]], str, str]:
    """挑推荐机构与时间：地点命中就用命中的那家，否则随机取至多三家。"""
    chooser = rng or random
    available = [hosp for hosp in schedule_info or [] if hosp.get("TenantName")]

    wanted = _split_location(location)
    matched = [hosp for hosp in available if hosp.get("TenantName") in wanted]

    if matched:
        selected = matched[: len(wanted)]
        first = matched[0]
        return selected, first.get("TenantName", ""), ""

    if len(available) >= 3:
        return chooser.sample(available, 3), "", ""
    return available.copy(), "", ""


def recommend_offline(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    location: str | None = None,
    date_input: str | None = None,
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    """线下处方：带上可选机构与推荐时段（医院名 + "YYYY/MM/DD HH:MM-HH:MM"）。"""
    chooser = rng or random
    target_date, time_period = parse_date_input(date_input)
    result: list[dict[str, Any]] = []

    for item, detail in pairs:
        schedule_info = _schedule_of(item, detail) or []
        selected, hospital_name, chosen_time = _choose_hospitals(schedule_info, location, chooser)

        if selected and not chosen_time:
            first = selected[0]
            hospital_name = first.get("TenantName", "")
            chosen_time = pick_slot(first.get("Schedule", {}), target_date, time_period, chooser)

        entry: dict[str, Any] = {
            "Id": detail.get("Id", item.get("Id")),
            "Name": detail.get("Name") or item.get("Name") or f"处方{detail.get('Id')}",
            "Indication": detail.get("Indication", ""),
            "Prescription": detail.get("Prescription", ""),
            "Hospitals": [
                {
                    "Name": hosp.get("TenantName"),
                    "occupied_slots": extract_occupied_slots(hosp.get("Schedule", {})),
                }
                for hosp in selected
            ],
            "Details": detail.get("TrainList", []),
            "time_slot_recommendation": [{"Hospital": hospital_name, "Time": chosen_time}],
        }
        if detail.get("Type") is not None:
            entry["Type"] = detail["Type"]
        result.append(entry)
    return result


def _reason_hint(reasons: Any) -> str:
    """单疾病 summary 前缀：「根据<理由>」。"""
    if not isinstance(reasons, dict) or not reasons:
        return ""
    first_key = next(iter(reasons))
    return f"根据{reasons[first_key]}"


def _online_names(items: list[dict[str, Any]]) -> list[str]:
    return [f"[{item.get('Name', '')}]" for item in items]


def _offline_texts(items: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for item in items:
        text = f"[{item.get('Name', '')}]"
        recommendation = (item.get("time_slot_recommendation") or [{}])[0]
        hospital = recommendation.get("Hospital", "")
        when = recommendation.get("Time", "")
        if hospital and when:
            text += f"，推荐地点：{hospital} 推荐时间：{when}"
        texts.append(text.strip())
    return texts


def single_output(
    online: list[dict[str, Any]],
    offline: list[dict[str, Any]],
    reasons: Any = None,
) -> dict[str, Any]:
    """单疾病输出：截断到 6/8/14，并拼一段「线上N个、线下N个」的说明。"""
    online = _unique(online)[:MAX_ONLINE]
    offline = _unique(offline)[:MAX_OFFLINE]
    if len(online) + len(offline) > MAX_TOTAL:
        offline = offline[: max(MAX_TOTAL - len(online), 0)]

    hint = _reason_hint(reasons)
    online_text = "；".join(_online_names(online)) or "无"
    offline_text = "；".join(_offline_texts(offline)) or "无"
    prefix = f"{hint}推荐相关处方。" if hint else ""
    summary = (
        f"{prefix}线上处方共{len(online)}个：{online_text}。"
        f"线下处方共{len(offline)}个：{offline_text}。"
    )
    return {"online_prescriptions": online, "offline_prescriptions": offline, "summary": summary}


def _related(
    items: list[dict[str, Any]], disease: str
) -> list[dict[str, Any]]:
    """按 Indication / Name 匹配疾病，命中不了再用疾病同义词兜底。"""
    matched = [
        item
        for item in items
        if disease in (item.get("Indication") or "") or disease in (item.get("Name") or "")
    ]
    if matched:
        return matched

    keywords = DISEASE_KEYWORDS.get(disease, ())
    if not keywords:
        return []
    text_matched = []
    for item in items:
        full = f"{item.get('Indication', '')} {item.get('Name', '')} {item.get('Prescription', '')}"
        if any(keyword in full for keyword in keywords):
            text_matched.append(item)
    return text_matched


def multi_output(
    online: list[dict[str, Any]],
    offline: list[dict[str, Any]],
    diseases: list[str],
    reasons: Any = None,
) -> dict[str, Any]:
    """多疾病输出：每个疾病单独分配处方（允许跨疾病重复），再合并去重并收口。"""
    per_online: dict[str, list[dict[str, Any]]] = {}
    per_offline: dict[str, list[dict[str, Any]]] = {}
    for disease in diseases:
        per_online[disease] = _unique(_related(online, disease))[:MAX_ONLINE]
        per_offline[disease] = _unique(_related(offline, disease))[:MAX_OFFLINE]

    merged_online: list[dict[str, Any]] = []
    merged_offline: list[dict[str, Any]] = []
    for disease in diseases:
        merged_online.extend(per_online[disease])
        merged_offline.extend(per_offline[disease])
    merged_online = _unique(merged_online)[:MAX_ONLINE]
    remaining = max(MAX_TOTAL - len(merged_online), 0)
    merged_offline = _unique(merged_offline)[: min(MAX_OFFLINE, remaining)]

    if not diseases:
        online_text = "；".join(_online_names(merged_online)) or "无"
        offline_text = "；".join(_online_names(merged_offline)) or "无"
        summary = (
            f"线上处方共{len(merged_online)}个：{online_text}。"
            f"线下处方共{len(merged_offline)}个：{offline_text}。"
        )
        return {
            "online_prescriptions": merged_online,
            "offline_prescriptions": merged_offline,
            "summary": summary,
        }

    parts: list[str] = []
    for disease in diseases:
        reason = reasons.get(disease, "") if isinstance(reasons, dict) else ""
        if reason:
            reason = reason if reason.endswith(("。", ".", "!", "?", "！", "？")) else (
                reason.rstrip("，,") + "。"
            )
            text = reason
        else:
            text = f"针对{disease}，"

        related_online = per_online[disease]
        related_offline = per_offline[disease]
        if related_online or related_offline:
            if related_online:
                names = "；".join(_online_names(related_online))
                text += f"线上处方共{len(related_online)}个：{names}；"
            else:
                text += "线上处方无；"
            if related_offline:
                names = "；".join(_online_names(related_offline))
                text += f"线下处方共{len(related_offline)}个：{names}。"
            else:
                text += "线下处方无。"
        else:
            text += "暂无相关处方推荐。"
        parts.append(text)

    return {
        "online_prescriptions": merged_online,
        "offline_prescriptions": merged_offline,
        "summary": " ".join(parts),
    }
