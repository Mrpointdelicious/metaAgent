"""
创建日期：2026-11-10
文件功能：用本机合成 HTTP 服务验证 MaxKB 接入探测、部署配置和错误脱敏。
"""

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import httpx
import pytest

from meta_agent.integrations.knowledge_probe import main
from tests.test_remote_knowledge import mock_http, response


@pytest.fixture
def endpoint(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("META_AGENT__") or name == "TEST_MAXKB_TOKEN":
            monkeypatch.delenv(name)
    state = SimpleNamespace(requests=[], status=200, body=response())

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            state.requests.append((self.path, dict(self.headers), json.loads(raw)))
            body = state.body
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}/v1/knowledge/search"
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def env_file(tmp_path, endpoint, **changes):
    source = {
        "id": "maxkb",
        "description": "合成 MaxKB 接口",
        "protocol": "knowledge-search-v1",
        "url": endpoint.url,
        "domains": ["health", "help", "product"],
        "scoped_domains": ["help", "product"],
        "token_env": "TEST_MAXKB_TOKEN",
        **changes,
    }
    path = tmp_path / ".env.probe"
    path.write_text(
        "META_AGENT__KNOWLEDGE_SOURCES=" + json.dumps([source]) + "\n"
        "TEST_MAXKB_TOKEN=secret-not-for-output\n",
        encoding="utf-8",
    )
    return path


def args(path, *extra):
    return [
        "--env-file",
        str(path),
        "--source",
        "maxkb",
        "--domain",
        "product",
        "--query",
        "介绍二楼",
        "--space-id",
        "hospital-a",
        *extra,
    ]


@pytest.mark.parametrize("status", ["ok", "partial", "no_evidence"])
def test_probe_module_uses_real_loopback_http_and_accepts_all_valid_statuses(
    tmp_path, endpoint, status
):
    endpoint.body = response(status)
    if status == "no_evidence":
        endpoint.body.update(context="", sources=[], gaps=["没有已发布资料"])
    path = env_file(tmp_path, endpoint)
    result = subprocess.run(
        [sys.executable, "-m", "meta_agent.integrations.knowledge_probe", *args(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["status"] == status and output["network_verified"]
    assert output["trace_id"] == "opaque-trace"
    assert output["source_count"] == (0 if status == "no_evidence" else 1)
    assert len(endpoint.requests) == 1
    route, headers, payload = endpoint.requests[0]
    assert route == "/v1/knowledge/search"
    assert headers["Authorization"] == "Bearer secret-not-for-output"
    assert headers["X-Request-ID"] == output["request_id"]
    assert payload == {
        "query": "介绍二楼",
        "domain": "product",
        "scope": {"space_id": "hospital-a"},
        "limits": {"max_context_tokens": 4000},
    }
    assert "secret-not-for-output" not in result.stdout + result.stderr
    assert "Synthetic published evidence" not in result.stdout


def test_check_config_does_not_claim_network_verification(tmp_path, endpoint, capsys):
    assert main(args(env_file(tmp_path, endpoint), "--check-config")) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["check"] == "configuration" and output["network_verified"] is False
    assert endpoint.requests == []


@pytest.mark.parametrize(
    "extra,code",
    [
        (["--source", "other"], "knowledge_source_not_configured"),
        (["--space-id", ""], "knowledge_space_required"),
        (["--query", " "], "invalid_probe_request"),
    ],
)
def test_bad_request_configuration_never_contacts_endpoint(tmp_path, endpoint, capsys, extra, code):
    assert main(args(env_file(tmp_path, endpoint), *extra)) == 2
    assert json.loads(capsys.readouterr().out)["code"] == code
    assert endpoint.requests == []


def test_missing_token_is_a_configuration_failure(tmp_path, endpoint, capsys):
    path = env_file(tmp_path, endpoint, token_env="UNCONFIGURED_KNOWLEDGE_PROBE_TOKEN")
    assert main(args(path)) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "knowledge_credential_missing"
    assert endpoint.requests == []


def test_invalid_legacy_config_does_not_print_embedded_secret(tmp_path, endpoint, capsys):
    path = env_file(tmp_path, endpoint, protocol="maxkb", maxkb={"password": "old-secret"})
    assert main(args(path)) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["code"] == "invalid_configuration"
    assert "secret" not in captured.out + captured.err
    assert endpoint.requests == []


@pytest.mark.parametrize("status", [302, 401, 403, 422, 429, 503])
def test_http_error_is_reported_without_retry_redirect_or_response_details(
    tmp_path, endpoint, capsys, status
):
    endpoint.status, endpoint.body = status, b"upstream-secret-and-private-details"
    assert main(args(env_file(tmp_path, endpoint))) == 3
    output = capsys.readouterr().out
    assert json.loads(output)["http_status"] == status
    assert "secret" not in output and "private-details" not in output
    assert len(endpoint.requests) == 1


def test_legacy_retrieve_response_is_not_mistaken_for_v1(tmp_path, endpoint, capsys):
    endpoint.body = {"chunks": [{"text": "private text"}], "trace_id": "old"}
    assert main(args(env_file(tmp_path, endpoint))) == 4
    output = capsys.readouterr().out
    assert json.loads(output)["code"] == "invalid_knowledge_response"
    assert "private text" not in output


@pytest.mark.parametrize(
    "error,code",
    [
        (httpx.ConnectError("private-connect-details"), "knowledge_unreachable"),
        (httpx.ReadTimeout("private-timeout-details"), "knowledge_timeout"),
    ],
)
def test_network_failure_has_no_fallback_or_sensitive_output(
    tmp_path, endpoint, capsys, monkeypatch, error, code
):
    attempts = []

    def fail(req):
        attempts.append(req)
        raise error

    mock_http(monkeypatch, fail)
    assert main(args(env_file(tmp_path, endpoint))) == 3
    output = capsys.readouterr().out
    assert json.loads(output)["code"] == code
    assert "private" not in output and len(attempts) == 1


def test_sample_configuration_keeps_knowledge_disabled_until_deployment(endpoint, capsys):
    path = Path(__file__).parents[1] / ".env.example"
    assert main(args(path, "--check-config")) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "knowledge_source_not_configured"
    assert endpoint.requests == []
