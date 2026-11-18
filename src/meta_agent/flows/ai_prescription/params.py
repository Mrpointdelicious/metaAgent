"""
创建日期：2026-11-16
文件功能：AI推荐处方的参数合并节点（对应 Dify「合并参数」代码节点）。

输入是 LLM 抽取的七个字段 + 上一轮累积的 params_json，输出统一的参数字典：
  - 新值优先、旧值兜底（都取不到就是 None）；
  - 训练模式 → 游戏库随机挑游戏（名称 / 图标）；
  - 游戏功能 → 设备类型与序列号；
  - 疾病个数 → is_multi_disease（0 未知 / 1 单疾病 / >=2 多疾病）；
  - 再把七个字段压成 params_json 字符串，供下一轮兜底用。
"""

from __future__ import annotations

import contextlib
import json
import random
import re
from typing import Any

# 游戏库：(显示名, 训练模式, 图标 key)，顺序与原节点一致。
GAME_LIBRARY: tuple[tuple[str, str, str], ...] = (
    ("轨迹训练", "单臂游戏", "track"),
    ("步步生花", "行走游戏", "Walk"),
    ("梦幻森林", "行走游戏", "dForest"),
    ("欢乐盖大楼", "坐站游戏", "gaidalou"),
    ("坐站训练", "坐站游戏", "Sit"),
    ("宇宙拼图", "平衡游戏", "Child"),
    ("太空飞行", "平衡游戏", "Plane"),
    ("太空漫步", "行走游戏", "sWalk"),
    ("中式庭院", "行走游戏", "zhongshi"),
    ("丛林探险", "行走游戏", "conglin"),
    ("海景别墅", "行走游戏", "SeaVilla"),
    ("汪汪天使", "坐站游戏", "Dog"),
    ("跑酷猫", "平衡游戏", "Cat"),
    ("打地鼠", "平衡游戏", "Mouse"),
    ("一转到底", "单臂游戏", "turns"),
    ("滚动高尔夫", "单臂游戏", "golf"),
    ("垃圾分类大作战", "单臂游戏", "sort"),
    ("镜像训练", "单臂游戏", "mirror"),
    ("舒尔特方格", "单臂游戏", "schulte"),
    ("足球训练", "单臂游戏", "football"),
    ("米字训练", "单臂游戏", "mizi"),
    ("摘水果", "单臂游戏", "pickFruit"),
    ("汉字书写", "单臂游戏", "write"),
    ("七巧板", "单臂游戏", "puzzle"),
    ("五子棋", "单臂游戏", "gomoku"),
    ("魔力几何配型", "单臂游戏", "geometry"),
    ("稳定极限", "平衡评估", "Stablity"),
    ("前庭感觉", "平衡评估", "Vetibular"),
    ("视觉评估", "平衡评估", "Eye"),
    ("本体感觉", "平衡评估", "Noumenon"),
    ("循迹", "行走游戏", "sTrail"),
)

LOCATING_MODES = ("行走游戏", "坐站游戏", "平衡游戏", "平衡评估")
_EMPTY_VALUES = (None, "", "null", "None", "无")


def parse_json_like(value: Any) -> dict[str, Any]:
    """尽量把模型/会话变量里的「像 JSON 的字符串」解析成 dict。

    原节点连续三次尝试：直接 loads（含嵌套字符串）→ 正则补引号后 loads →
    unicode_escape 还原后 loads；全失败就返回空字典。
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value

    text = str(value).strip()
    if text.startswith("```"):
        text = text[text.find("\n") + 1 :]
    if text.endswith("```"):
        text = text[: text.rfind("\n")]

    for _ in range(3):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, str):
            text = parsed
            continue

        try:
            fixed = re.sub(r'(".*?")\s*:\s*([^"{}\[\],\n]+)', r'\1: "\2"', text)
            parsed = json.loads(fixed)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed

        with contextlib.suppress(AttributeError, TypeError, ValueError, UnicodeDecodeError):
            text = json.loads(text.encode("utf-8").decode("unicode_escape"))
        if isinstance(text, dict):
            return text

    return {}


def _choose(new: dict[str, Any], old: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """先在新值里找，再退到旧值；空串 / "null" / "无" 都算没填。"""
    for source in (new, old):
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if value not in _EMPTY_VALUES:
                return value
    return None


def pick_game_by_mode(mode: Any, rng: random.Random | None = None) -> tuple[str, str, str]:
    """训练模式（单个或列表）→ (游戏功能, 游戏名, 图标)，多个用逗号拼接。"""
    if not mode:
        return "", "", ""

    modes = (
        [str(item).strip() for item in mode if str(item).strip()]
        if isinstance(mode, list)
        else [str(mode).strip()]
    )

    chooser = rng or random
    funcs: list[str] = []
    names: list[str] = []
    icons: list[str] = []
    for current in modes:
        candidates = [game for game in GAME_LIBRARY if game[1] == current]
        funcs.append(current)
        if candidates:
            name, _, icon = chooser.choice(candidates)
            names.append(name)
            icons.append(icon)
        else:
            names.append("")
            icons.append("")
    return ",".join(funcs), ",".join(names), ",".join(icons)


def device_for(game_func: str) -> tuple[str | None, str | None]:
    """游戏功能 → (设备类型, 序列号)；单臂优先，其余下肢设备共用一个占位序列号。"""
    if "单臂游戏" in game_func:
        return "iReMo", "iReMo-H000000000"
    if any(mode in game_func for mode in LOCATING_MODES):
        return "iReGo", "iReGo-H000000000"
    return None, None


def _disease_and_multi(disease: Any) -> tuple[str, int]:
    """疾病字段 → (展示名, 疾病个数)；未知/空统一按 0 处理。"""
    if not disease:
        return "未知", 0
    if isinstance(disease, list):
        names = [str(item) for item in disease if str(item).strip()]
        if len(names) == 1:
            return names[0], 0 if names[0] == "未知" else 1
        return ", ".join(names), sum(1 for name in names if name != "未知")
    text = str(disease)
    return text, 0 if text == "未知" else 1


def _reasons(value: Any) -> dict[str, Any]:
    """疾病判断依据统一成对象：dict 原样、list/str 包一层 reason。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"reason": value}
    if isinstance(value, str) and value.strip():
        return {"reason": value}
    return {"reason": []}


def merge_params(
    extraction: Any,
    old_params: Any = "",
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """合并本轮抽取结果与旧会话参数，返回可直接下发的参数字典。"""
    new = parse_json_like(extraction)
    old = parse_json_like(old_params)

    disease_raw = _choose(new, old, ("疾病", "disease"))
    reasons = _reasons(_choose(new, old, ("疾病判断依据", "disease_reasons")))
    train_mode = _choose(new, old, ("训练模式", "training_mode"))
    location = _choose(new, old, ("地点", "location"))
    time_value = _choose(new, old, ("时间", "time"))
    scale = _choose(new, old, ("量表", "scale"))

    game_func, game_name, game_icon = pick_game_by_mode(train_mode, rng)
    device_type, serial_number = device_for(game_func)
    disease, is_multi = _disease_and_multi(disease_raw)

    params_json = json.dumps(
        {
            "disease": disease,
            "disease_reasons": reasons,
            "is_multi_disease": is_multi,
            "location": location,
            "time": time_value,
            "scale": scale,
        },
        ensure_ascii=False,
    )

    return {
        "disease": disease,
        "disease_reasons": reasons,
        "is_multi_disease": is_multi,
        "location": location,
        "time": time_value,
        "scale": scale,
        "DeviceType": device_type,
        "GameFunc": game_func,
        "GameName": game_name,
        "GameIcon": game_icon,
        "SerialNumber": serial_number,
        "params_json": params_json,
    }


def disease_list(disease: str, reasons: dict[str, Any]) -> list[str]:
    """多疾病路径用：把 "a, b" 拆回列表；拆不出来就退回判断依据的 key。"""
    names = [item.strip() for item in str(disease or "").split(",") if item.strip()]
    if names:
        return names
    if isinstance(reasons, dict):
        return [str(key) for key in reasons if str(key).strip()]
    return []
