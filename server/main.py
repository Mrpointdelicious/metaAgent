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
from server.request_factory import build_orchestrator_request_from_payload
from server.session_context import MissingIdentityContextError


def handle_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Formal service adapter: frontend payload -> request factory -> orchestrator.

    Raw multi-turn history is not concatenated here. The request factory
    standardizes session_id/conversation_id, and the Agent runtime uses
    session_id to fetch the SDK session store.
    """

    try:
        request = build_orchestrator_request_from_payload(payload)
    except MissingIdentityContextError as exc:
        return {
            "success": False,
            "task_type": payload.get("task_type") or "unknown",
            "structured_output": {"error": "missing_identity_context"},
            "final_text": "Missing identity context: request must include doctor_id or patient_id.",
            "validation_issues": [str(exc)],
            "execution_trace": [],
        }

    response = RehabAgentOrchestrator(get_settings()).run(request)
    payload_response = response.model_dump(mode="json")
    if request.identity_context is not None:
        payload_response["session_id"] = request.identity_context.session_id
        payload_response["conversation_id"] = request.identity_context.conversation_id
    return payload_response


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
