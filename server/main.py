from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import RehabAgentOrchestrator
from config import get_settings

from server.session_context import MissingIdentityContextError, build_orchestrator_request_from_payload


def handle_payload(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        request = build_orchestrator_request_from_payload(payload)
    except MissingIdentityContextError as exc:
        return {
            "success": False,
            "task_type": payload.get("task_type") or "unknown",
            "structured_output": {"error": "missing_identity_context"},
            "final_text": "缺少身份上下文：请求必须包含 doctor_id 或 patient_id。",
            "validation_issues": [str(exc)],
            "execution_trace": [],
        }
    response = RehabAgentOrchestrator(get_settings()).run(request)
    return response.model_dump(mode="json")


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"success": False, "error": "empty_payload"}, ensure_ascii=False))
        return 1
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        print(json.dumps({"success": False, "error": "payload_must_be_object"}, ensure_ascii=False))
        return 1
    print(json.dumps(handle_payload(payload), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
