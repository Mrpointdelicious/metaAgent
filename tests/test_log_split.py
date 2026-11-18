# 创建日期：2026-11-18
# 文件功能：验证日志按客户端令牌（client）分文件落盘；主文件写全量、分文件只写对应令牌。
import json
import logging

from meta_agent.observability.logging import configure_logging


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
