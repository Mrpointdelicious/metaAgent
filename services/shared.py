from __future__ import annotations

import ast
import json
from datetime import datetime, time, timedelta
from typing import Any

from models import TimeRange, TrainingTask
from repositories import RehabRepository

PLACEHOLDER_DATETIME = datetime(1900, 1, 1)

REPORT_MODE_LABELS = {
    1: "walk",
    2: "sitstand",
    3: "game",
    4: "balance",
}

TEMPLATE_MODE_LABELS = {
    0: "walk",
    1: "sitstand",
    2: "balance_game",
    3: "assessment",
}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_json_field(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text:
        return None
    candidates = [text]
    try:
        parsed = json.loads(text)
        if isinstance(parsed, str):
            candidates.append(parsed)
        else:
            return parsed
    except json.JSONDecodeError:
        pass
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            try:
                return ast.literal_eval(candidate)
            except (ValueError, SyntaxError):
                continue
    return None


def parse_datetime_flexible(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return None if raw <= PLACEHOLDER_DATETIME else raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                value = datetime.strptime(text, fmt)
                return None if value <= PLACEHOLDER_DATETIME else value
            except ValueError:
                continue
        try:
            value = datetime.fromisoformat(text)
            return None if value <= PLACEHOLDER_DATETIME else value
        except ValueError:
            return None
    return None


def build_time_range(
    repository: RehabRepository,
    *,
    patient_id: int | None = None,
    therapist_id: int | None = None,
    days: int = 30,
    start: datetime | None = None,
    end: datetime | None = None,
    prefer_walk_anchor: bool = False,
) -> TimeRange:
    end_value = resolve_time_anchor(
        repository,
        patient_id=patient_id,
        therapist_id=therapist_id,
        prefer_walk_anchor=prefer_walk_anchor,
        explicit_end=end,
    )
    if start is None:
        start_date = (end_value - timedelta(days=days)).date()
        start_value = datetime.combine(start_date, time.min)
    else:
        start_value = start
    label = f"{start_value.date().isoformat()} to {end_value.date().isoformat()}"
    return TimeRange(start=start_value, end=end_value, label=label)


def resolve_time_anchor(
    repository: RehabRepository,
    *,
    patient_id: int | None = None,
    therapist_id: int | None = None,
    prefer_walk_anchor: bool = False,
    explicit_end: datetime | None = None,
) -> datetime:
    end_value = explicit_end
    if end_value is None:
        if prefer_walk_anchor and patient_id is not None:
            end_value = repository.get_walk_anchor(patient_id=patient_id)
        end_value = end_value or repository.get_plan_anchor(patient_id=patient_id, therapist_id=therapist_id)
        end_value = end_value or datetime.now()
    if end_value.hour == 0 and end_value.minute == 0 and end_value.second == 0:
        end_value = end_value + timedelta(days=1) - timedelta(seconds=1)
    return end_value


def parse_training_tasks(raw: Any) -> list[TrainingTask]:
    payload = parse_json_field(raw)
    if payload is None:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    tasks: list[TrainingTask] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        mode = item.get("templateMode")
        task_name = item.get("GameNameCh") or item.get("gameName") or item.get("GameCh")
        category = TEMPLATE_MODE_LABELS.get(safe_int(mode), item.get("GameCh"))
        task_code = str(item.get("gameName") or task_name or "unknown").lower()
        tasks.append(
            TrainingTask(
                task_code=task_code,
                task_name=task_name,
                category=category,
                template_mode=safe_int(mode) if mode is not None else None,
                planned_time_min=safe_float(item.get("time")),
                speed=safe_float(item.get("speed"), default=0.0) if item.get("speed") is not None else None,
                assistance=safe_float(item.get("assistance"), default=0.0) if item.get("assistance") is not None else None,
                resistance=safe_float(item.get("resistance"), default=0.0) if item.get("resistance") is not None else None,
                sit_time=safe_float(item.get("sitTime"), default=0.0) if item.get("sitTime") is not None else None,
                stand_time=safe_float(item.get("standTime"), default=0.0) if item.get("standTime") is not None else None,
                weight_loss=safe_float(item.get("weightLoss"), default=0.0) if item.get("weightLoss") is not None else None,
                selected_index=safe_int(item.get("selectedIndex")) if item.get("selectedIndex") is not None else None,
            )
        )
    return tasks


def task_catalog(tasks: list[TrainingTask]) -> list[str]:
    seen: list[str] = []
    for task in tasks:
        label = task.task_name or task.task_code
        if label and label not in seen:
            seen.append(label)
    return seen


def parse_report_entries(raw: Any) -> list[dict[str, Any]]:
    payload = parse_json_field(raw)
    if not isinstance(payload, dict):
        return []
    entries = payload.get("ReportList")
    return entries if isinstance(entries, list) else []


def summarize_report_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    total_training_minutes = 0.0
    walk_distance = 0.0
    sit_count = 0
    balance_time = 0.0
    game_score = 0.0
    modes: list[str] = []
    for entry in entries:
        report_mode = safe_int(entry.get("ReportMode"), default=-1)
        mode_label = REPORT_MODE_LABELS.get(report_mode, f"mode_{report_mode}")
        if mode_label not in modes:
            modes.append(mode_label)
        total_training_minutes += safe_float(entry.get("WTrainingWalktime"))
        total_training_minutes += safe_float(entry.get("STrainingSitTime"))
        total_training_minutes += safe_float(entry.get("RTrainingBalanceTime"))
        total_training_minutes += safe_float(entry.get("GTrainingTime"))
        walk_distance += safe_float(entry.get("WDistance"))
        sit_count += safe_int(entry.get("SNumber"))
        balance_time += safe_float(entry.get("RTrainingBalanceTime"))
        game_score += safe_float(entry.get("GScore"))
    return {
        "total_training_minutes": round(total_training_minutes, 2),
        "walk_distance": round(walk_distance, 2),
        "sit_count": sit_count,
        "balance_time": round(balance_time, 2),
        "game_score": round(game_score, 2),
        "detail_modes": modes,
    }


def average(values: list[float]) -> float | None:
    cleaned = [value for value in values if value is not None]
    if not cleaned:
        return None
    return sum(cleaned) / len(cleaned)


def format_ratio(value: float | None) -> str:
    if value is None:
        return "NA"
    return f"{value * 100:.1f}%"


def format_number(value: float | None) -> str:
    if value is None:
        return "NA"
    return f"{value:.1f}"
