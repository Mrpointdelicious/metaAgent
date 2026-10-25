"""
创建日期：2026-10-22
文件功能：发现 Compose 配置、核验镜像版本、排空运行并事务式切换 Prompt 配置。
"""

import argparse
import contextlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

KEY = "META_AGENT__PROMPT_BUNDLE"
CATALOG_CODE = (
    "import json;from meta_agent.prompts.service import PromptService;"
    "print(json.dumps(PromptService().releases()))"
)
OPS_CODE = """
import json,sys,urllib.request
from meta_agent.config import Settings
args=json.load(sys.stdin)
data=json.dumps(args['data']).encode() if args.get('data') is not None else None
request=urllib.request.Request('http://127.0.0.1:8080/_ops/prompt-deployment',
    data=data,headers={'Authorization':'Bearer '+Settings().service_bearer_token,
                      'Content-Type':'application/json'})
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(request,timeout=10) as response:
    print(response.read().decode())
"""


class SwitchError(RuntimeError):
    pass


def command(args, *, payload=None, timeout=90):
    """Capture all Docker output; failed commands never echo configuration or secrets."""
    try:
        result = subprocess.run(
            ["docker", *args],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SwitchError("Docker command unavailable or timed out") from exc
    if result.returncode:
        raise SwitchError(f"Docker {args[0]} failed (exit {result.returncode})")
    return result.stdout


def parse_json(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise SwitchError("Docker did not return valid deployment metadata") from exc


def render_env(original: bytes, bundle: str) -> bytes:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", bundle):
        raise SwitchError("Invalid bundle name")
    text = original.decode("utf-8-sig")
    pattern = re.compile(r"^[ \t]*(?:export[ \t]+)?" + KEY + r"[ \t]*=[^\r\n]*", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if len(matches) > 1:
        raise SwitchError(f"Duplicate {KEY} entries; resolve before switching")
    newline = "\r\n" if "\r\n" in text else "\n"
    if matches:
        match = matches[0]
        # Keep the existing line ending and every unrelated byte, including credentials.
        text = text[: match.start()] + KEY + "=" + bundle + text[match.end() :]
    else:
        text += ("" if not text or text.endswith("\n") else newline) + KEY + "=" + bundle + newline
    return (b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8")


def atomic_write(path: Path, content: bytes):
    permissions = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    descriptor, name = tempfile.mkstemp(prefix=".prompt-switch-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(permissions)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def deployment_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SwitchError("Another prompt switch is already running") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class Deployment:
    def __init__(self, container="meta_agent", timeout=120, runner=command):
        self.container, self.timeout, self.run = container, timeout, runner
        self.original_container = parse_json(self.run(["inspect", container]))[0]
        labels = self.original_container["Config"].get("Labels", {})
        project = labels.get("com.docker.compose.project")
        files = labels.get("com.docker.compose.project.config_files", "").split(",")
        if not project or not all(files):
            raise SwitchError("The target is not a Compose-managed container")
        self.compose = ["compose", "--project-name", project]
        for path in labels.get("com.docker.compose.project.environment_file", "").split(","):
            if path:
                self.compose.extend(["--env-file", path])
        for path in files:
            self.compose.extend(["-f", path])
        self.service = labels["com.docker.compose.service"]
        config = parse_json(
            self.run([*self.compose, "config", "--format", "json", "--no-env-resolution"])
        )
        service = config["services"][self.service]
        if KEY in service.get("environment", {}):
            raise SwitchError(f"Compose environment overrides {KEY}; use an env_file for switching")
        env_files = service.get("env_file", [])
        if not env_files:
            raise SwitchError("The target service has no runtime env_file")
        last = env_files[-1]
        self.env_file = Path(last["path"] if isinstance(last, dict) else last)
        if not self.env_file.is_absolute():
            self.env_file = Path(labels["com.docker.compose.project.working_dir"]) / self.env_file
        if not self.env_file.is_file():
            raise SwitchError("The runtime env_file does not exist")
        self.image = service["image"]
        self.image_id = parse_json(self.run(["image", "inspect", self.image]))[0]["Id"]
        self.output = (
            Path(labels["com.docker.compose.project.working_dir"]) / ".local/prompt-switch"
        )

    def releases(self):
        return parse_json(
            self.run(
                [
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--entrypoint",
                    "python",
                    self.image,
                    "-c",
                    CATALOG_CODE,
                ]
            )
        )

    def ops(self, draining=None):
        return parse_json(
            self.run(
                ["exec", "-i", self.container, "python", "-c", OPS_CODE],
                payload={"data": {"draining": draining} if draining is not None else None},
                timeout=20,
            )
        )

    def recreate(self, rollback_image=None):
        args = list(self.compose)
        if rollback_image:
            override = self.output / "rollback-image.json"
            atomic_write(
                override,
                json.dumps({"services": {self.service: {"image": rollback_image}}}).encode(),
            )
            args.extend(["-f", str(override)])
        self.run(
            [
                *args,
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                str(self.timeout),
                self.service,
            ],
            timeout=self.timeout + 30,
        )

    def verify(self, bundle, expected_hash, image_id):
        state = self.ops()
        current = parse_json(self.run(["inspect", self.container]))[0]
        if (
            not state["ready"]
            or state["draining"]
            or state["bundle"] != bundle
            or state["bundle_hash"] != expected_hash
            or current["Image"] != image_id
        ):
            raise SwitchError("The running service did not load the requested prompt release")
        return state

    def switch(self, bundle):
        with deployment_lock(self.output / "switch.lock"):
            releases = {r["bundle"]: r for r in self.releases()}
            if bundle not in releases:
                raise SwitchError(f"Bundle {bundle} is not available in the configured image")
            original = self.env_file.read_bytes()
            updated = render_env(original, bundle)
            before = self.ops()
            if before["draining"]:
                raise SwitchError("The service is already draining; finish that deployment first")
            expected = releases[bundle]["bundle_hash"]
            if (
                before["bundle"] == bundle
                and before["bundle_hash"] == expected
                and self.original_container["Image"] == self.image_id
                and original == updated
            ):
                return {"status": "unchanged", "bundle": bundle, "bundle_hash": expected}
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            backup = self.output / stamp
            backup.mkdir(parents=True)
            atomic_write(backup / "runtime-before.env", original)
            record = {"from": before["bundle"], "to": bundle, "image_id": self.image_id}
            changed, drained = False, False
            try:
                print("Waiting for current runs to finish...", flush=True)
                drained = True
                state = self.ops(True)
                deadline = time.monotonic() + self.timeout
                while state["active_runs"] or state["starting_runs"]:
                    if time.monotonic() >= deadline:
                        raise SwitchError(
                            "Timed out waiting for active runs; no configuration changed"
                        )
                    time.sleep(1)
                    state = self.ops()
                if self.env_file.read_bytes() != original:
                    raise SwitchError(
                        "Runtime env_file changed during drain; retry with the new config"
                    )
                changed = True
                atomic_write(self.env_file, updated)
                print(f"Recreating {self.service} with {bundle}...", flush=True)
                self.recreate()
                self.verify(bundle, expected, self.image_id)
                result = {"status": "switched", "bundle": bundle, "bundle_hash": expected}
                record.update(result)
                atomic_write(backup / "result.json", json.dumps(record, indent=2).encode())
                return result
            except BaseException:
                try:
                    if changed:
                        atomic_write(self.env_file, original)
                        self.recreate(rollback_image=self.original_container["Image"])
                    if drained:
                        self.ops(False)
                    self.verify(
                        before["bundle"], before["bundle_hash"], self.original_container["Image"]
                    )
                except Exception as recovery:
                    record["status"] = "recovery_failed"
                    atomic_write(backup / "result.json", json.dumps(record).encode())
                    raise SwitchError(
                        f"Switch failed; automatic recovery failed. Inspect {backup}"
                    ) from recovery
                record["status"] = "rolled_back"
                atomic_write(backup / "result.json", json.dumps(record).encode())
                raise SwitchError(
                    "Switch failed; previous configuration and runtime restored"
                ) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--bundle")
    choice.add_argument("--list", action="store_true")
    parser.add_argument("--container", default="meta_agent")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    try:
        deployment = Deployment(args.container, args.timeout)
        result = (
            [
                {key: release[key] for key in ("bundle", "bundle_hash", "released_on", "change")}
                for release in deployment.releases()
            ]
            if args.list
            else deployment.switch(args.bundle)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (SwitchError, OSError, ValueError) as exc:
        print(f"Prompt switch failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
