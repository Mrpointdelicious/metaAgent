import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from meta_agent.application.report_history import historical_images, read_history_image, report_turn
from meta_agent.tools.ai_webapi import normalize_artifact_url

REF = "irego_scope_v2_" + "a" * 64
GRANT = "patient_artifact_v2.payload." + "b" * 43
BASE = "http://lan:15043/aiwebapi/ai/patients/report-artifacts/" + REF + "/content"
DIRECT = BASE.replace("/aiwebapi/", "/api/")


def test_saved_and_legacy_turns_read_by_ref_in_the_original_message():
    text = "[Cweb]" + BASE + "?access_grant=" + GRANT + "\n[web]https://front/chat"
    turn = report_turn("question", text, 100)
    assert GRANT not in turn["assistant"]
    rows = [{"id": "c:1", "answer": turn["assistant"], "report_bindings": turn["report_bindings"]}, {"id": "c:2", "answer": text}]
    client = SimpleNamespace(read_report_image=AsyncMock(return_value=b"png"))
    scope = object()
    for i in ("c:1", "c:2"):
        assert asyncio.run(read_history_image(rows, REF, i, scope, client, "c")) == b"png"
    assert asyncio.run(read_history_image(rows, REF, "wrong-id", scope, client, "c")) is None
    client.read_report_image.assert_awaited_with(REF, GRANT, scope, "c")
    historical_images(rows, lambda u: normalize_artifact_url(SimpleNamespace(), u))
    assert [row["answer"] for row in rows] == ["[Cweb]" + DIRECT + "\n[web]https://front/chat"] * 2
    assert all(row["report_artifacts"] == [REF] and "report_bindings" not in row for row in rows)


def test_15043_direct_and_proxy_prefixes():
    settings = SimpleNamespace(ai_webapi_artifact_path_prefix="aiwebapi")
    assert normalize_artifact_url(settings, BASE) == DIRECT
    assert normalize_artifact_url(settings, DIRECT + "?access_grant=old") == DIRECT + "?access_grant=old"
    assert normalize_artifact_url(settings, DIRECT.replace(":15043", ":9443")) == BASE.replace(":15043", ":9443")
