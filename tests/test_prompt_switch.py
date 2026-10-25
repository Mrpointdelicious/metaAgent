"""
创建日期：2026-10-22
文件功能：验证 Prompt 发布冻结、排空竞态、配置保护及部署失败恢复。
"""

import asyncio
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from meta_agent.app import create_app
from meta_agent.contracts import DomainError
from meta_agent.prompts.contracts import SpacePromptInputs
from meta_agent.prompts.service import PromptService
from tests.helpers.runtime import request, runtime
from tools.prompt_switch import KEY, Deployment, SwitchError, deployment_lock, render_env


def test_published_baseline_is_frozen_and_v2_changes_only_current_room_rules():
    prompts = PromptService()
    versions = {r["bundle"]: r for r in prompts.releases()}
    assert versions["builtin-v1"]["bundle_hash"] == (
        "a6ec735d80f1ff6cd8e3cb842e6c9aae8dd3156a829223ca146a5748db0d69f7"
    )
    baseline = prompts.bind("builtin-v1")
    updated = prompts.bind("builtin-v2")
    assert [a.prompt_id for a in baseline.definitions] == [b.prompt_id for b in updated.definitions]
    for old, new in zip(baseline.definitions, updated.definitions, strict=True):
        assert (old.template_hash != new.template_hash) == (old.prompt_id == "space.context")
    rendered = prompts.render(
        updated, "space.context", SpacePromptInputs(current_space={"scene_id": None})
    )
    assert "本轮未提供 scene_id" in rendered.system_message.content
    assert "不因本轮换房间而判定上一轮回答错误" in rendered.system_message.content


def test_rewriting_a_published_prompt_is_rejected_before_startup():
    from dataclasses import replace

    prompts = PromptService()
    original = prompts.bind()
    modified = replace(original.definitions[0], template_hash="changed")
    prompts._catalog = {
        **prompts._catalog,
        "builtin-v1": replace(original, definitions=(modified, *original.definitions[1:])),
    }
    with pytest.raises(ValueError, match="Published prompt bundle changed"):
        prompts.releases()


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("has_entry", [False, True])
def test_env_update_preserves_credentials_comments_unicode_and_line_endings(newline, has_entry):
    lines = [b"# private config", b"TOKEN='private$`value#keep'", "SPACE=岳阳医院".encode(), b""]
    if has_entry:
        lines.append((KEY + "='builtin-v1' # current").encode())
    lines.append(b"OTHER=untouched")
    original = newline.join(lines) + newline
    result = render_env(original, "builtin-v2")
    assert b"TOKEN='private$`value#keep'" + newline in result
    assert "SPACE=岳阳医院".encode() + newline + newline in result
    assert b"OTHER=untouched" + newline in result
    assert (KEY + "=builtin-v2").encode() + newline in result
    assert render_env(result, "builtin-v2") == result


def test_duplicate_or_invalid_env_values_fail_without_returning_secrets():
    with pytest.raises(SwitchError, match="Duplicate"):
        render_env(f"{KEY}=v1\n{KEY}=v2\nSECRET=private".encode(), "builtin-v2")
    with pytest.raises(SwitchError, match="Invalid bundle"):
        render_env(b"SECRET=private", "bundle\nSECRET=changed")


def test_ops_requires_loopback_and_service_identity(client, settings, auth_headers):
    assert client.get("/_ops/prompt-deployment", headers=auth_headers).status_code == 403
    with TestClient(create_app(settings), client=("127.0.0.1", 55000)) as local:
        assert local.get("/_ops/prompt-deployment").status_code == 401
        response = local.get("/_ops/prompt-deployment", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["bundle_hash"] == PromptService().bind().bundle_hash
        assert {r["bundle"] for r in response.json()["releases"]} == {
            "builtin-v1",
            "builtin-v2",
            "builtin-v3",
        }


def test_drain_preserves_existing_snapshots_and_rejects_only_new_runs(settings, auth_headers):
    with TestClient(create_app(settings), client=("127.0.0.1", 55000)) as client:
        payload = {
            "query": "你好",
            "request_id": "before-drain",
            "user": "actor",
            "response_mode": "blocking",
        }
        original = client.post("/v1/chat", headers=auth_headers, json=payload).json()
        assert (
            client.post(
                "/_ops/prompt-deployment", headers=auth_headers, json={"draining": True}
            ).status_code
            == 200
        )
        reused = client.post("/v1/chat", headers=auth_headers, json=payload)
        assert reused.status_code == 200 and reused.json()["run_id"] == original["run_id"]
        new = {**payload, "request_id": "during-drain"}
        assert client.post("/v1/chat", headers=auth_headers, json=new).status_code == 503
        assert (
            client.post(
                "/_ops/prompt-deployment", headers=auth_headers, json={"draining": False}
            ).status_code
            == 200
        )
        assert client.post("/v1/chat", headers=auth_headers, json=new).status_code == 200


def test_run_registration_in_flight_is_counted_until_it_can_finish(monkeypatch):
    async def scenario():
        async with runtime() as (container, _):
            service = container.application
            original = service._build_run
            entered, release = asyncio.Event(), asyncio.Event()

            async def paused(*args):
                entered.set()
                await release.wait()
                return await original(*args)

            monkeypatch.setattr(service, "_build_run", paused)
            pending = asyncio.create_task(service.start(request("你好")))
            await entered.wait()
            assert service.starting_runs == 1 and not service.active
            service.draining = True
            with pytest.raises(DomainError) as rejected:
                await service.start(request("你好", "new-request"))
            assert rejected.value.code == "service_draining"
            release.set()
            run = await pending
            await run.task
            assert service.starting_runs == 0 and not service.active
            assert run.record.status == "succeeded"

    asyncio.run(scenario())


class FakeDeployment(Deployment):
    def __init__(self, root, *, fail=False):
        self.env_file = root / "runtime.env"
        self.env_file.write_bytes(f"PRIVATE=never-print\n{KEY}=builtin-v1\n".encode())
        self.output = root / ".local/prompt-switch"
        self.state = {
            "ready": True,
            "bundle": "builtin-v1",
            "bundle_hash": "hash-v1",
            "active_runs": 0,
            "starting_runs": 0,
            "draining": False,
        }
        self.image_id, self.original_container = "image-one", {"Image": "image-one"}
        self.timeout, self.service, self.fail = 1, "meta_agent", fail
        self.recreated = []
        self.drained = []

    def releases(self):
        return [
            {"bundle": "builtin-v1", "bundle_hash": "hash-v1"},
            {"bundle": "builtin-v2", "bundle_hash": "hash-v2"},
        ]

    def ops(self, draining=None):
        if draining is not None:
            self.drained.append(draining)
            self.state["draining"] = draining
        return deepcopy(self.state)

    def recreate(self, rollback_image=None):
        self.recreated.append(rollback_image)
        if self.fail and rollback_image is None:
            raise SwitchError("Simulated deployment failure")
        bundle = "builtin-v1" if b"=builtin-v1" in self.env_file.read_bytes() else "builtin-v2"
        self.state.update(
            bundle=bundle,
            bundle_hash="hash-v1" if bundle.endswith("v1") else "hash-v2",
            draining=False,
        )

    def verify(self, bundle, expected_hash, image_id):
        assert (self.state["bundle"], self.state["bundle_hash"]) == (bundle, expected_hash)
        assert image_id == self.image_id and not self.state["draining"]


def test_switch_changes_only_bundle_and_can_switch_back_without_rebuilding(tmp_path, capsys):
    deployment = FakeDeployment(tmp_path)
    assert deployment.switch("builtin-v2")["status"] == "switched"
    assert deployment.switch("builtin-v2")["status"] == "unchanged"
    assert deployment.switch("builtin-v1")["status"] == "switched"
    assert deployment.recreated == [None, None]
    assert "PRIVATE=never-print" in deployment.env_file.read_text()
    assert "never-print" not in capsys.readouterr().out


def test_unknown_bundle_does_not_change_config_or_drain_service(tmp_path):
    deployment = FakeDeployment(tmp_path)
    original = deployment.env_file.read_bytes()
    with pytest.raises(SwitchError, match="not available"):
        deployment.switch("missing")
    assert deployment.env_file.read_bytes() == original
    assert deployment.drained == [] and deployment.recreated == []


def test_failed_recreation_restores_exact_config_and_original_image(tmp_path):
    deployment = FakeDeployment(tmp_path, fail=True)
    original = deployment.env_file.read_bytes()
    with pytest.raises(SwitchError, match="previous configuration and runtime restored"):
        deployment.switch("builtin-v2")
    assert deployment.env_file.read_bytes() == original
    assert deployment.recreated == [None, "image-one"]
    assert not deployment.state["draining"]
    record = next(deployment.output.glob("*/result.json"))
    assert json.loads(record.read_text())["status"] == "rolled_back"
    assert "never-print" not in record.read_text()


def test_busy_runs_timeout_without_recreating_or_changing_env(tmp_path, monkeypatch):
    deployment = FakeDeployment(tmp_path)
    deployment.state["active_runs"] = 1
    original = deployment.env_file.read_bytes()
    times = iter([0, 2])
    monkeypatch.setattr("tools.prompt_switch.time.monotonic", lambda: next(times))
    with pytest.raises(SwitchError, match="previous configuration and runtime restored"):
        deployment.switch("builtin-v2")
    assert deployment.env_file.read_bytes() == original and not deployment.recreated
    assert deployment.drained == [True, False]


def test_concurrent_config_edit_is_preserved_instead_of_overwritten(tmp_path, monkeypatch):
    deployment = FakeDeployment(tmp_path)
    original_ops = deployment.ops

    def edited(draining=None):
        if draining is True:
            deployment.env_file.write_bytes(b"PRIVATE=operator-updated\n")
        return original_ops(draining)

    monkeypatch.setattr(deployment, "ops", edited)
    with pytest.raises(SwitchError, match="previous configuration and runtime restored"):
        deployment.switch("builtin-v2")
    assert deployment.env_file.read_bytes() == b"PRIVATE=operator-updated\n"
    assert not deployment.recreated and not deployment.state["draining"]


def test_concurrent_switches_are_rejected(tmp_path):
    path = tmp_path / "switch.lock"
    with (
        deployment_lock(path),
        pytest.raises(SwitchError, match="already running"),
        deployment_lock(path),
    ):
        pass
