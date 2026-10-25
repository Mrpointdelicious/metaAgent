"""
创建日期：2026-10-22
文件功能：验证报告公网地址的容器内检查，以及 Dify 图片链接与展示去重。
"""

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from meta_agent.tools.ai_webapi import AIWebApiClient
from tests.helpers.settings import AppTestSettings


@pytest.mark.parametrize("area", ["patients", "hospital-operations"])
def test_public_report_probes_internal_backend_without_changing_display_url(area):
    async def check():
        seen = []
        path = f"/api/ai/{area}/report-artifacts/report-123/content"
        public_url = "http://localhost:15043" + path + "?format=png"

        def handler(request):
            seen.append(request)
            if request.method == "HEAD":
                return httpx.Response(405)
            return httpx.Response(200, headers={"content-type": "image/png"})

        client = AIWebApiClient(
            AppTestSettings(
                ai_webapi_base_url="http://patient-backend:8080/api/ai/patients/tools",
                ai_webapi_hospital_base_url="http://hospital-backend:8081/api/ai/hospital/tools",
                artifact_public_base_url="http://localhost:15043/",
                ai_webapi_bearer_token="synthetic-secret",
            ),
            httpx.MockTransport(handler),
        )
        try:
            assert await client.artifact_available(public_url)
            assert [r.method for r in seen] == ["HEAD", "GET"]
            expected = "patient-backend" if area == "patients" else "hospital-backend"
            assert all(r.url.host == expected and r.url.path == path for r in seen)
            assert all(r.url.query == b"format=png" for r in seen)
            assert all("authorization" not in r.headers for r in seen)
            assert public_url.startswith("http://localhost:15043/")
            seen.clear()
            for bad in [
                public_url.replace(":15043", ":15044"),
                public_url.replace("localhost", "other.test"),
                public_url.replace("localhost", "user:pass@localhost"),
                "http://localhost:15043/api/ai/patients/tools/get_patient_profile",
            ]:
                assert not await client.artifact_available(bad)
            assert not seen
        finally:
            await client.close()

    asyncio.run(check())


@pytest.mark.parametrize("kind", ["redirect", "html", "timeout"])
def test_internal_report_probe_rejects_invalid_responses(kind):
    async def check():
        def handler(request):
            if kind == "timeout":
                raise httpx.ConnectTimeout("synthetic", request=request)
            if kind == "redirect":
                return httpx.Response(302, headers={"location": "http://other.test/report"})
            return httpx.Response(200, headers={"content-type": "text/html"})

        client = AIWebApiClient(
            AppTestSettings(artifact_public_base_url="http://localhost:15043"),
            httpx.MockTransport(handler),
        )
        try:
            assert not await client.artifact_available(
                "http://localhost:15043/api/ai/patients/report-artifacts/report-123/content"
            )
        finally:
            await client.close()

    asyncio.run(check())


@pytest.mark.parametrize("answer_kind", ["no_url", "link", "image", "image_with_title"])
def test_dify_renders_verified_image_even_when_answer_contains_text_link(answer_kind):
    spec = importlib.util.spec_from_file_location(
        "dify_present", Path(__file__).parents[1] / "workflows/dify/present.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    url = "http://localhost:15043/api/ai/patients/report-artifacts/report-123/content"
    answer = {
        "no_url": "报告已生成。",
        "link": f"[查看报告]({url})",
        "image": f"![报告]({url})",
        "image_with_title": f'![报告]({url} "报告图片")',
    }[answer_kind]
    event = {"type": "artifact_ready", "payload": {"url": url}}
    body = {
        "status": "succeeded",
        "events": [{"type": "answer_part", "payload": {"text": answer}}, event, event],
    }
    output = module.main(json.dumps(body), 200)["display_text"]
    assert output.count("![") == 1
    assert url in output
