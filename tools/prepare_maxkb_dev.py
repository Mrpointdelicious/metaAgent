"""Prepare private Docker settings using the existing local MaxKB deployment."""

import argparse
import json
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def env_values(path):
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, val = line.split("=", 1)
                values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maxkb-dir", type=Path, default=Path("D:/Project/MetaRag"))
    args = parser.parse_args()
    upstream = args.maxkb_dir.resolve() / ".local/maxkb"
    release_path = upstream / "release.json"
    release = json.loads(release_path.read_text(encoding="utf8"))
    access = json.loads((upstream / "access.json").read_text(encoding="utf8"))
    model = env_values(upstream / "agent.env") | env_values(ROOT / ".env")
    key = model.get("META_AGENT__DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("Existing answer-model credential is missing")
    folder = ROOT / ".local/maxkb-dev"
    folder.mkdir(parents=True, exist_ok=True)
    prior = (
        json.loads((folder / "access.json").read_text(encoding="utf8"))
        if (folder / "access.json").exists()
        else {}
    )
    token = prior.get("service_token") or secrets.token_urlsafe(40)
    values = {
        "APP_ENV": "test",
        "DRY_RUN": "true",
        "PERSISTENCE_BACKEND": "memory",
        "SERVICE_BEARER_TOKEN": token,
        "PLANNER_MODE": "deterministic",
        "DEFAULT_TENANT_ID": release["scope_key"],
        "MAXKB_ENABLED": "true",
        "MAXKB_URL": "http://host.docker.internal:18082/v1/experimental/hybrid/retrieve",
        "MAXKB_TOKEN": access["retrieval_token"],
        "MAXKB_RELEASE_PATH": "/run/maxkb/release.json",
        "MAXKB_TOP_K": "3",
        "MAXKB_TIMEOUT_SECONDS": "20",
        "MAXKB_ANSWER_MODE": "llm",
        "MAXKB_ANSWER_TIMEOUT_SECONDS": "30",
        "DEEPSEEK_API_KEY": key,
        "DEEPSEEK_BASE_URL": model.get("META_AGENT__DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "ANSWER_MODEL": model.get("META_AGENT__ANSWER_MODEL", "deepseek:deepseek-chat"),
        "REQUEST_TIMEOUT_SECONDS": "60",
    }
    (folder / "agent.env").write_text(
        "".join("META_AGENT__" + k + "=" + v + "\n" for k, v in values.items()), encoding="utf8"
    )
    (folder / "compose.env").write_text(
        "MAXKB_RELEASE_FILE=" + release_path.as_posix() + "\n", encoding="utf8"
    )
    (folder / "access.json").write_text(
        json.dumps(
            {
                "service_token": token,
                "space_id": access["space_id"],
                "tenant_id": release["scope_key"],
                "url": "http://127.0.0.1:18084/compat/dify/v1/chat-messages",
                "release": release["corpus_version"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf8",
    )
    print(json.dumps({"prepared": True, "port": 18084, "release": release["corpus_version"]}))


if __name__ == "__main__":
    main()
