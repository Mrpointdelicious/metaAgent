"""Publish a native rerank-only MaxKB application using an existing native model.

Only MaxKB application credentials enter the Agent config. Configure vendor
credentials in MaxKB model management before running this tool.
"""

import argparse
import asyncio
import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from meta_agent.domains.maxkb_client import MaxKBClient  # noqa: E402
from meta_agent.domains.maxkb_contract import canonical  # noqa: E402
from meta_agent.domains.maxkb_rerank import WorkflowConfig  # noqa: E402

MARKER = "Managed by metaAgent native-rerank-v1"
NAME = "MetaAgent Native Rerank"


def write_private(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


async def configure(config_path, admin_url, agent_admin_url, model_id):
    UUID(model_id)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    flow = json.loads((ROOT / "docs/examples/maxkb-rerank-workflow.json").read_text("utf8"))
    flow = copy.deepcopy(flow)
    for node in flow["nodes"]:
        if node["id"] == "base-node":
            node["properties"]["node_data"].update(name=NAME, desc=MARKER, prologue="")
        if node["type"] == "reranker-node":
            node["properties"]["node_data"]["reranker_model_id"] = model_id
    digest = hashlib.sha256(canonical(flow)).hexdigest()
    manifest_path = config_path.parent / "rerank-application.json"
    prior = json.loads(manifest_path.read_text("utf8")) if manifest_path.exists() else {}
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=45) as http:
        api = MaxKBClient(http, admin_url, config["username"], config["password"])
        models = await api.call("GET", "/workspace/default/model")
        model = next((m for m in models if m["id"] == model_id), None)
        if not model or model["model_type"] != "RERANKER":
            raise ValueError("Select a native MaxKB RERANKER model")
        applications = await api.call("GET", "/workspace/default/application")
        matches = [a for a in applications if a["name"] == NAME]
        if len(matches) > 1:
            raise ValueError("Multiple native rerank applications need an explicit cleanup")
        application = matches[0] if matches else None
        if application and application.get("desc") != MARKER:
            raise ValueError("Refusing to overwrite an application not managed by this tool")
        if not application:
            application = await api.call(
                "POST",
                "/workspace/default/application",
                json={
                    "name": NAME,
                    "desc": MARKER,
                    "type": "WORK_FLOW",
                    "folder_id": "default",
                    "prologue": "",
                    "work_flow": flow,
                },
            )
        app_id = application["id"]
        base = f"/workspace/default/application/{app_id}"
        current = await api.call("GET", base)
        # Disable optional features; keep only the native reranker and JSON reply.
        must_publish = (
            prior.get("application_id") != app_id
            or prior.get("workflow_sha256") != digest
            or not current.get("is_publish")
            or hashlib.sha256(canonical(current["work_flow"])).hexdigest() != digest
        )
        if must_publish:
            await api.call(
                "PUT",
                base,
                json={
                    "name": NAME,
                    "desc": MARKER,
                    "work_flow": flow,
                    "knowledge_id_list": [],
                    "long_term_enable": False,
                    "file_upload_enable": False,
                    "mcp_enable": False,
                    "tool_enable": False,
                    "application_enable": False,
                    "clean_time": 1,
                    "file_clean_time": 1,
                },
            )
            await api.call("PUT", base + "/publish", json={})
        # Anonymous sharing is not needed for this internal application. Native
        # application API keys have a separate auth handler and remain usable.
        await api.call(
            "PUT",
            base + "/access_token",
            json={
                "is_active": False,
                "show_exec": False,
                "show_source": False,
            },
        )
        existing = config.get("rerank", {})
        if existing.get("application_id") == app_id and existing.get("api_key"):
            app_key = existing["api_key"]
        else:
            key = await api.call("POST", base + "/application_key", json={})
            app_key = key["secret_key"]
        parsed = urlsplit(agent_admin_url)
        config["rerank"] = {
            "enabled": True,
            "api_base": f"{parsed.scheme}://{parsed.netloc}/chat/api",
            "api_key": app_key,
            "application_id": app_id,
            "timeout_seconds": 8,
        }
        WorkflowConfig.model_validate(config["rerank"])
        versions = await api.call("GET", base + "/application_version")
        manifest = {
            "application_id": app_id,
            "model_id": model_id,
            "workflow_sha256": digest,
            "published_version_id": versions[0]["id"] if versions else None,
            "sharing_enabled": False,
            "long_term_memory": False,
        }
        write_private(config_path, config)
        write_private(manifest_path, manifest)
        return {**manifest, "configured": True, "vendor_credential_copied_to_agent": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".local/maxkb-dev/native.json")
    parser.add_argument("--admin-url", default="http://127.0.0.1:18081/admin/api")
    parser.add_argument("--agent-admin-url", default="http://host.docker.internal:18081/admin/api")
    parser.add_argument("--model-id", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(
            configure(args.config, args.admin_url, args.agent_admin_url, args.model_id)
        )
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        # MaxKB/provider error bodies and native credentials must not reach CLI logs.
        print(json.dumps({"configured": False, "error": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
