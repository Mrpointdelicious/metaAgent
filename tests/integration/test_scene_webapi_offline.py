"""
创建日期：2026-10-30
文件功能：显式启用后通过真实AI_WebApi和合成目录验证聊天至场景的HTTP链路。
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from meta_agent.agent.runtime import SingleAgentRuntime
from meta_agent.app import create_app
from meta_agent.orchestration.identity import trusted_scope_from_inputs
from tests.helpers.settings import AppTestSettings
from tests.test_single_agent import ScriptedModel, tool_call

ROOT = os.environ.get("META_AGENT_TEST_SCENE_WEBAPI_ROOT", "")
pytestmark = pytest.mark.skipif(not ROOT, reason="需要显式配置本地合成AI_WebApi测试项目")


@pytest.fixture(scope="module")
def scene_server(tmp_path_factory):
    root = Path(ROOT).resolve()
    path = root / "tools/verify_scene_command_resolver.py"
    spec = importlib.util.spec_from_file_location("local_scene_smoke", path)
    smoke = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(smoke)
    finally:
        sys.dont_write_bytecode = previous
    dll = root / "AI_WebApi/bin/Debug/net6.0/AI_WebApi.dll"
    assert dll.is_file(), "先构建本地AI_WebApi测试项目"
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), smoke.CatalogStub)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    output = tmp_path_factory.mktemp("scene-webapi-offline")
    base = f"http://127.0.0.1:{port}"
    upstream_url = f"http://127.0.0.1:{upstream.server_port}"
    env = {
        **os.environ,
        "ASPNETCORE_ENVIRONMENT": "Production",
        "IReGoTools__DifyServiceBearerToken": smoke.TOKEN,
        "MainDB": "unused-synthetic-database",
        "RehabTools__DefaultSpaceId": "smoke-space",
        "RehabTools__Scene__VersionUrl": upstream_url + "/version",
        "RehabTools__Scene__QueryUrl": upstream_url + "/query",
        "RehabTools__Scene__ApiKey": "smoke-key",
        "RehabTools__Scene__ApiToken": "smoke-token",
        "Logging__LogLevel__Default": "Warning",
    }
    with (output / "host.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                "dotnet",
                "--roll-forward",
                "LatestMajor",
                str(dll),
                "--urls",
                base,
                "--contentRoot",
                str(output),
            ],
            cwd=output,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + 25
            while True:
                assert process.poll() is None, "本地合成WebApi启动失败，查看测试临时目录host.log"
                try:
                    smoke.request(base, "/api/ai/rehab/tools/openapi.json")
                    break
                except (URLError, ConnectionError):
                    if time.monotonic() > deadline:
                        pytest.fail("本地合成WebApi启动超时")
                    time.sleep(0.1)
            yield base, smoke.TOKEN
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=5)


@pytest.mark.parametrize("protocol", ["native", "dify-blocking", "dify-streaming"])
@pytest.mark.parametrize("version", [321, 322])
def test_chat_to_real_webapi_with_synthetic_catalog(scene_server, protocol, version):
    base, token = scene_server
    settings = AppTestSettings(
        app_env="test",
        dry_run=False,
        service_bearer_token="synthetic-chat",
        scene_actions_enabled=True,
        dify_workflow_events_enabled=True,
        ai_webapi_base_url=base + "/api/ai/patients/tools",
        ai_webapi_bearer_token=token,
    )
    query = "关闭地图然后关闭训练记录面板"
    answer = (
        "已解析两个动作，客户端执行结果待确认。"
        if version == 321
        else "场景版本已变化，请刷新后重试。"
    )
    with TestClient(create_app(settings)) as client:
        container = client.app.state.container
        container.application.agent_runtime = SingleAgentRuntime(
            ScriptedModel(
                messages=iter(
                    [
                        tool_call("navigate_scene", {"target": query}),
                        AIMessage(answer),
                    ]
                )
            )
        )
        payload = {
            "user": "synthetic-user",
            "query": query,
            "inputs": {"space_id": "smoke-space", "scene_version": version},
            "response_mode": "streaming" if protocol == "dify-streaming" else "blocking",
        }
        endpoint = "/v1/chat" if protocol == "native" else "/v1/chat-messages"
        if protocol == "native":
            payload["request_id"] = "offline-scene"
        result = client.post(
            endpoint, headers={"Authorization": "Bearer synthetic-chat"}, json=payload
        )
        assert result.status_code == 200
        if protocol == "native":
            body = result.json()
            actions = [
                event["payload"]["command_code"]
                for event in body["events"]
                if event["type"] == "action_ready"
            ]
            assert actions == (["[ClickPoint:]22", "[ClickPoint:]31"] if version == 321 else [])
            if version != 321:
                scope = trusted_scope_from_inputs(
                    payload["inputs"], payload["user"], settings.default_tenant_id
                )
                record = client.portal.call(
                    container.repository.run, scope.scope_hash, body["run_id"]
                )
                assert next(iter(record.results.values())).code == "scene_version_mismatch"
            delivery = body["action_delivery"]
        else:
            if protocol == "dify-streaming":
                frames = [
                    json.loads(line[6:])
                    for line in result.text.splitlines()
                    if line.startswith("data: ")
                ]
                assert [frame["event"] for frame in frames] == [
                    "workflow_started",
                    "node_started",
                    "node_finished",
                    "message",
                    "workflow_finished",
                    "message_end",
                ]
                body = frames[3]
            else:
                body = result.json()
            assert answer in body["answer"]
            delivery = body["metadata"]["action_delivery"]
        if version == 321:
            assert len(delivery) == 2 and set(delivery.values()) == {"delivery_unknown"}
        else:
            assert not delivery
