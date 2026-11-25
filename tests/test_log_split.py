# 创建日期：2026-11-18
# 文件功能：验证日志按客户端令牌（client）分文件落盘；主文件写全量、分文件只写对应令牌。
import json
import logging

from meta_agent.observability.logging import (
    JsonFormatter,
    configure_logging,
    redact_request_inputs,
)


def test_split_by_client(tmp_path):
    log_dir = tmp_path / "logs"
    configure_logging("INFO", log_dir=str(log_dir))
    root = logging.getLogger()
    try:
        logging.getLogger("probe.main").info("hello main")
        logging.getLogger("probe.p").info(
            "rx run",
            extra={
                "run_id": "r1",
                "event_type": "ai_prescription_completed",
                "fields": {"client": "prescription"},
            },
        )
        for handler in root.handlers:
            handler.flush()
        main = (log_dir / "meta_agent.log").read_text(encoding="utf-8").strip().splitlines()
        assert len(main) == 2
        assert json.loads(main[1])["client"] == "prescription"
        split = (
            (log_dir / "meta_agent.prescription.log")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()
        )
        assert len(split) == 1
        assert json.loads(split[0])["run_id"] == "r1"
        assert not (log_dir / "meta_agent.primary.log").exists()
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()


def test_json_formatter_uses_local_timezone():
    """日志时间戳默认带 +08:00，不再是难核对的 UTC。"""
    fmt = JsonFormatter(8)
    rec = logging.LogRecord("probe", logging.INFO, "", 0, "hello", None, None)
    payload = json.loads(fmt.format(rec))
    assert payload["timestamp"].endswith("+08:00")
    # UTC 时间与本地时间相差 8 小时，本地小时不应等于 datetime.now(UTC) 的小时。
    from datetime import UTC, datetime

    assert payload["timestamp"][:13] != datetime.now(UTC).strftime("%Y-%m-%dT%H")


def test_json_formatter_zero_offset_is_utc():
    fmt = JsonFormatter(0)
    rec = logging.LogRecord("probe", logging.INFO, "", 0, "hello", None, None)
    assert json.loads(fmt.format(rec))["timestamp"].endswith("+00:00")


def test_request_summary_fields_and_redaction():
    """request_summary 能带 query/inputs/answer/status，且输入脱敏。"""
    redacted = redact_request_inputs(
        {
            "requesterphone": "15932910402",
            "patientphone": "13800001111",
            "identity_context_token": "SECRET-TOKEN",
            "apikey": "SECRET-KEY",
            "pos": "一楼 大厅",
        }
    )
    assert redacted["requesterphone"] == "159****0402"
    assert redacted["patientphone"] == "138****1111"
    assert "identity_context_token" not in redacted
    assert "apikey" not in redacted
    assert redacted["pos"] == "一楼 大厅"

    fmt = JsonFormatter(8)
    rec = logging.LogRecord("meta_agent.application.service", logging.INFO, "", 0,
                            "request_summary", None, None)
    rec.run_id = "run_x"
    rec.event_type = "request_summary"
    rec.fields = {
        "client": "realtime-analysis",
        "flow": "ai_realtime_analysis",
        "status": "succeeded",
        "elapsed_ms": 100.0,
        "query": "查步道报告",
        "inputs": json.dumps(redacted, ensure_ascii=False),
        "answer": "大爷，这是智能步道记录。",
    }
    payload = json.loads(fmt.format(rec))
    assert payload["query"] == "查步道报告"
    assert payload["answer"].startswith("大爷")
    assert payload["status"] == "succeeded"
    assert "SECRET" not in json.dumps(payload, ensure_ascii=False)


def test_redact_non_dict():
    assert redact_request_inputs(None) == {}
    assert redact_request_inputs("not a dict") == {}
