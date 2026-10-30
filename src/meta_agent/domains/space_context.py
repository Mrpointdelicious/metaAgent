"""
创建日期：2026-10-22
文件功能：根据 RAG 已有场景说明快照校验空间、房间归属和当前位置上下文。
"""

import asyncio
import json
import time
from functools import lru_cache
from importlib.resources import files
from uuid import UUID

import httpx

_remote_enabled = False
_remote_catalog: dict = {}
_last_refresh = 0.0
_refresh_lock = asyncio.Lock()


def configure_scene_catalog(enabled: bool):
    global _remote_enabled, _remote_catalog, _last_refresh
    _remote_enabled, _remote_catalog, _last_refresh = enabled, {}, 0.0


async def refresh_scene_catalog(settings, *, required=False):
    global _remote_catalog, _last_refresh
    if not _remote_enabled:
        return
    if time.monotonic() - _last_refresh < settings.portfolio_scene_catalog_ttl_seconds:
        return
    async with _refresh_lock:
        if time.monotonic() - _last_refresh < settings.portfolio_scene_catalog_ttl_seconds:
            return
        try:
            token = settings.portfolio_help_token.get_secret_value()
            async with httpx.AsyncClient(
                timeout=min(5, settings.portfolio_timeout_seconds)
            ) as client:
                r = await client.get(
                    settings.portfolio_base_url.rstrip("/") + "/v1/scene-catalogs",
                    headers={"Authorization": "Bearer " + token},
                )
                r.raise_for_status()
                if len(r.content) > settings.max_tool_response_bytes:
                    raise ValueError("oversized_catalog")
                data = r.json()
            if not isinstance(data, dict):
                raise ValueError("invalid_catalog")
            for key, value in data.items():
                if (
                    not isinstance(key, str)
                    or not isinstance(value, dict)
                    or not isinstance(value.get("scenes"), dict)
                ):
                    raise ValueError("invalid_space")
                UUID(value["snapshot_id"])
                UUID(value["source_version_id"])
                if any(
                    not isinstance(name, str)
                    or not name.strip()
                    or len(name) > 160
                    or (floor is not None and type(floor) is not int)
                    for name, floor in value["scenes"].items()
                ):
                    raise ValueError("invalid_scene")
            _remote_catalog, _last_refresh = data, time.monotonic()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            if required:
                raise RuntimeError("scene_catalog_unavailable") from exc


async def scene_catalog_loop(settings):
    while True:
        await refresh_scene_catalog(settings)
        await asyncio.sleep(settings.portfolio_scene_catalog_ttl_seconds)


@lru_cache(maxsize=1)
def _offline_catalog() -> dict:
    return json.loads(
        files("meta_agent.domains").joinpath("space_catalog.json").read_text(encoding="utf-8")
    )


def space_catalog() -> dict:
    return _remote_catalog if _remote_enabled else _offline_catalog()


def current_space_context(space_id: str | None, scene_id: str | None) -> dict | None:
    if scene_id and not space_id:
        raise ValueError("提供 sceneId 时必须同时提供 spaceId")
    registered = space_catalog().get(space_id)
    if scene_id and (registered is None or scene_id not in registered["scenes"]):
        raise ValueError("sceneId 必须是当前 spaceId 在 RAG 中已有的房间或场景名称")
    if registered is None:
        return None
    return {
        "space_id": space_id,
        "space_name": registered["name"],
        "scene_id": scene_id,
        "floor": registered["scenes"].get(scene_id),
        "source_locator": registered["source_locator"],
        "source_version_id": registered["source_version_id"],
        "catalog_snapshot_id": registered.get("snapshot_id"),
        "catalog_release_id": registered.get("release_id"),
    }
