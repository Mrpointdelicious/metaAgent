"""Prepare a bridge-free deployment without resurrecting cleared model keys."""

import argparse
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def env_values(path):
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_private(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def prepare(upstream, folder, *, maxkb_url, public_url, answer_mode, data_volume):
    release_path = upstream / "release.json"
    release = json.loads(release_path.read_text("utf8"))
    filename = release["catalog_file"]
    if Path(filename).name != filename:
        raise ValueError("Invalid catalog filename")
    if not (upstream / filename).is_file() or not (upstream / "originals").is_dir():
        raise ValueError("Published catalog and originals are required")
    access = json.loads((upstream / "access.json").read_text("utf8"))
    folder.mkdir(parents=True, exist_ok=True)
    prior = (
        json.loads((folder / "access.json").read_text("utf8"))
        if (folder / "access.json").exists()
        else {}
    )
    model = env_values(folder / "agent.env")
    native_path = folder / "native.json"
    native = json.loads(native_path.read_text("utf8")) if native_path.exists() else {}
    # Preserve this deployment's MaxKB application auth only, never a provider key.
    rerank = native.get("rerank")
    if rerank and set(rerank) - {
        "enabled",
        "api_base",
        "api_key",
        "application_id",
        "timeout_seconds",
    }:
        raise ValueError("Unexpected native rerank configuration fields")
    if rerank:
        parsed = urlsplit(maxkb_url)
        rerank = {**rerank, "api_base": f"{parsed.scheme}://{parsed.netloc}/chat/api"}
    # Retain only this deployment's existing answer credential; never read old
    # source .env, archives, bridge.json or another application's model key.
    token = prior.get("service_token") or secrets.token_urlsafe(40)
    values = {
        "APP_ENV": "test",
        "DRY_RUN": "true",
        "PERSISTENCE_BACKEND": "memory",
        "SERVICE_BEARER_TOKEN": token,
        "PLANNER_MODE": "deterministic",
        "DEFAULT_TENANT_ID": release["scope_key"],
        "MAXKB_ENABLED": "true",
        "MAXKB_URL": maxkb_url,
        "MAXKB_CONFIG_PATH": "/run/maxkb/native.json",
        "MAXKB_PUBLIC_BASE_URL": public_url,
        "MAXKB_RELEASE_PATH": "/run/maxkb/release.json",
        "MAXKB_TOP_K": "3",
        "MAXKB_TIMEOUT_SECONDS": "20",
        "MAXKB_ANSWER_MODE": answer_mode,
        "MAXKB_ANSWER_TIMEOUT_SECONDS": "30",
        "DEEPSEEK_API_KEY": model.get("META_AGENT__DEEPSEEK_API_KEY", ""),
        "DEEPSEEK_BASE_URL": model.get("META_AGENT__DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "ANSWER_MODEL": model.get("META_AGENT__ANSWER_MODEL", "deepseek:deepseek-chat"),
        "REQUEST_TIMEOUT_SECONDS": "60",
    }
    if any("\n" in str(v) or "\r" in str(v) for v in values.values()):
        raise ValueError("Multiline environment values are not supported")
    write_private(
        folder / "native.json",
        json.dumps(
            {
                "username": access["username"],
                "password": access["password"],
                **({"rerank": rerank} if rerank else {}),
            },
            ensure_ascii=False,
        ),
    )
    write_private(
        folder / "agent.env", "".join(f"META_AGENT__{k}={v}\n" for k, v in values.items())
    )
    compose = {
        "MAXKB_RELEASE_FILE": release_path.resolve().as_posix(),
        "MAXKB_CATALOG_FILE": (upstream / filename).resolve().as_posix(),
        "MAXKB_CATALOG_NAME": filename,
        "MAXKB_ORIGINALS_DIR": (upstream / "originals").resolve().as_posix(),
        "MAXKB_DATA_VOLUME": data_volume,
    }
    write_private(folder / "compose.env", "".join(f"{k}={v}\n" for k, v in compose.items()))
    result = {
        "service_token": token,
        "space_id": release["space_ids"][0],
        "tenant_id": release["scope_key"],
        "release": release["corpus_version"],
        "url": public_url.rstrip("/") + "/compat/dify/v1/chat-messages",
        "retrieve_url": public_url.rstrip("/") + "/v1/knowledge/retrieve",
        "answer_mode": answer_mode,
    }
    write_private(folder / "access.json", json.dumps(result, ensure_ascii=False, indent=2))
    return {
        "prepared": True,
        "integration": "native",
        "answer_mode": answer_mode,
        "answer_key_configured": bool(values["DEEPSEEK_API_KEY"]),
        "release": release["corpus_version"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maxkb-dir", type=Path, default=Path("D:/Project/MetaRag"))
    parser.add_argument("--maxkb-url", default="http://host.docker.internal:18081/admin/api")
    parser.add_argument("--public-url", default="http://127.0.0.1:18084")
    parser.add_argument(
        "--answer-mode", choices=["disabled", "extractive", "llm"], default="disabled"
    )
    parser.add_argument("--data-volume", default="metarag-maxkb_maxkb_data")
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(
                args.maxkb_dir.resolve() / ".local/maxkb",
                ROOT / ".local/maxkb-dev",
                maxkb_url=args.maxkb_url,
                public_url=args.public_url,
                answer_mode=args.answer_mode,
                data_volume=args.data_volume,
            )
        )
    )


if __name__ == "__main__":
    main()
