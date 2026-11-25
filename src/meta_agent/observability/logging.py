"""
创建日期：2026-08-29
文件功能：提供不记录患者原始数据的 JSON 结构化日志配置；支持按客户端令牌分文件落盘。
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

SAFE_FIELDS = frozenset(
    {
        "step",
        "method",
        "status",
        "http_status",
        "error",
        "error_type",
        "endpoint",
        "code",
        "tool",
        "tools",
        "count",
        "category",
        "role",
        "agent_id",
        "profile_version",
        "flow",
        "capability",
        "decision",
        "success",
        "has_token",
        "has_result",
        "call",
        "tool_calls",
        "llm_calls",
        "elapsed_ms",
        "answer_length",
        "answer",
        "client",
        "stage",
        # request_summary 摘要字段（开关打开时才是明文，见 Settings.log_payload）
        # answer/status/flow/elapsed_ms 已在上方白名单，这里只补真正新增的字段。
        "query",
        "inputs",
        "error_code",
        "error_message",
    }
)

# 请求摘要里的输入参数白名单：只展示这些字段，且敏感值打码。
# 凭证类字段一律不写入日志（值替换为 <redacted>），手机号打码中间段。
_REQUEST_INPUT_FIELDS = (
    "requesterphone",
    "requesterPhone",
    "patientphone",
    "patientPhone",
    "identity",
    "role",
    "pos",
    "timestamp",
    "spaceid",
    "spaceId",
    "serialNumber",
    "sceneVersion",
    "loginName",
    "loginname",
)
_REDACTED_INPUT_FIELDS = frozenset(
    {
        "token",
        "webyuntoken",
        "apikey",
        "apitoken",
        "maxkbauth",
        "identity_context_token",
        "patient_identity",
        "patientidentity",
        "signature",
        "sign",
    }
)
_PHONE_FIELDS = frozenset({"requesterphone", "patientphone", "phone"})


def redact_request_inputs(inputs: Any) -> dict[str, Any]:
    """构造可落盘的输入摘要：白名单字段 + 凭证剔除 + 手机号打码。

    仅用于诊断，不含临床明细；即使 log_payload 打开也不会泄露凭证或完整手机号。
    """
    if not isinstance(inputs, dict):
        return {}
    result: dict[str, Any] = {}
    for key in _REQUEST_INPUT_FIELDS:
        if key not in inputs:
            continue
        value = inputs[key]
        if isinstance(value, (dict, list)) or not isinstance(value, (str, int, float)):
            continue
        if key.lower() in _REDACTED_INPUT_FIELDS:
            result[key] = "<redacted>"
        elif key.lower() in _PHONE_FIELDS and isinstance(value, str) and len(value) >= 7:
            result[key] = value[:3] + "****" + value[-4:]
        else:
            result[key] = value
    return result


class JsonFormatter(logging.Formatter):
    """将标准日志记录转换为单行 JSON。

    timestamp 使用配置的固定时区偏移（默认北京时间 UTC+8），不再是难核对的 UTC。
    """

    def __init__(self, timezone_offset_hours: int = 8) -> None:
        super().__init__()
        self._tz = timezone(timedelta(hours=timezone_offset_hours))

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(self._tz).isoformat(),
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
            payload.update(
                {
                    key: value
                    for key, value in fields.items()
                    if key in SAFE_FIELDS and isinstance(value, (str, int, float, bool, type(None)))
                }
            )
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
    timezone_offset_hours: int = 8,
) -> None:
    """配置根日志器；调用方不得把患者数据放入日志消息。

    log_dir 为空时仅输出 stdout；配置后额外写滚动 JSON 文件（meta_agent.log / .1 / .2…），
    并按客户端令牌（client）分文件：meta_agent.<client>.log（同样滚动），
    便于事后按 run_id / event_type / client 排查。
    """
    formatter = JsonFormatter(timezone_offset_hours)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(_handler(logging.StreamHandler(), formatter))
    if log_dir:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        main_handler = RotatingFileHandler(
            directory / log_file_name,
            maxBytes=log_max_bytes,
            backupCount=log_backup_count,
            encoding="utf-8",
        )
        root.addHandler(_handler(main_handler, formatter))
        root.addHandler(
            _handler(
                ClientSplitHandler(directory, log_file_name, log_max_bytes, log_backup_count),
                formatter,
            )
        )
    root.setLevel(level.upper())
    for name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


class ClientSplitHandler(logging.Handler):
    """把带 client 标识的日志额外写入 meta_agent.<client>.log（主文件照常写全量）。

    client 来源优先级：record.fields["client"] > CURRENT_RUN 上下文（agent 循环内）。
    取不到 client 的记录不写分文件。
    """

    def __init__(
        self,
        directory: Path,
        file_name: str,
        max_bytes: int,
        backup_count: int,
    ) -> None:
        super().__init__()
        self._directory = directory
        self._stem = Path(file_name).stem
        self._suffix = Path(file_name).suffix or ".log"
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._handlers: dict[str, RotatingFileHandler] = {}
        self._create_lock = threading.Lock()

    def _client_of(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict) and fields.get("client"):
            return str(fields["client"])
        try:  # 延迟导入避免循环依赖；agent 循环内每条日志都能取到发起令牌
            from meta_agent.agent.tools import CURRENT_RUN

            ctx = CURRENT_RUN.get(None)
            client = getattr(getattr(ctx, "scope", None), "client_id", "")
            return str(client) if client else ""
        except Exception:  # 上下文不可用时归主文件
            return ""

    def _handler_for(self, client: str) -> RotatingFileHandler:
        handler = self._handlers.get(client)
        if handler is None:
            with self._create_lock:
                handler = self._handlers.get(client)
                if handler is None:
                    handler = RotatingFileHandler(
                        self._directory / f"{self._stem}.{client}{self._suffix}",
                        maxBytes=self._max_bytes,
                        backupCount=self._backup_count,
                        encoding="utf-8",
                    )
                    handler.setFormatter(self.formatter)
                    self._handlers[client] = handler
        return handler

    def emit(self, record: logging.LogRecord) -> None:
        client = self._client_of(record)
        if not client:
            return
        try:
            self._handler_for(client).emit(record)
        except Exception:  # 分文件失败不影响主链路
            self.handleError(record)


def _handler(handler: logging.Handler, formatter: logging.Formatter) -> logging.Handler:
    handler.setFormatter(formatter)
    return handler
