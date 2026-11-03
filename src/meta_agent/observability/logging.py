"""
创建日期：2026-08-29
文件功能：提供不记录患者原始数据的 JSON 结构化日志配置。
"""

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    """将标准日志记录转换为单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("trace_id", "run_id", "event_type", "dependency"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else "unknown"
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(
    level: str,
    *,
    log_dir: str = "",
    log_file_name: str = "meta_agent.log",
    log_max_bytes: int = 10 * 1024 * 1024,
    log_backup_count: int = 5,
) -> None:
    """配置根日志器；调用方不得把患者数据放入日志消息。

    log_dir 为空时仅输出 stdout；配置后额外写滚动 JSON 文件（meta_agent.log / .1 / .2…），
    便于事后按 run_id / event_type 排查。
    """
    formatter = JsonFormatter()
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(_handler(logging.StreamHandler(), formatter))
    if log_dir:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            directory / log_file_name,
            maxBytes=log_max_bytes,
            backupCount=log_backup_count,
            encoding="utf-8",
        )
        root.addHandler(_handler(file_handler, formatter))
    root.setLevel(level.upper())
    for name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _handler(handler: logging.Handler, formatter: logging.Formatter) -> logging.Handler:
    handler.setFormatter(formatter)
    return handler
