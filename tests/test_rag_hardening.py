"""
创建日期：2026-10-23
文件功能：验证全局医疗检索、批量子问题预算、动态场景与新提示词约束。
"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from meta_agent.config import Settings
from meta_agent.contracts import DomainError
from meta_agent.domains.portfolio import PortfolioKnowledgeAdapter
from meta_agent.domains.space_context import (
    configure_scene_catalog,
    current_space_context,
    refresh_scene_catalog,
)
from meta_agent.prompts.service import PromptService


@pytest.mark.parametrize("space", [None, "unregistered-hospital", "上海中医药大学附属岳阳医院"])
def test_medical_queries_ignore_space_and_support_bounded_subquestions(monkeypatch, space):
    async def check():
        medical = str(uuid4())
        hospital = str(uuid4())
        observed = []
        settings = Settings(
            _env_file=None,
            portfolio_token="medical-reader",
            portfolio_kb_ids=[medical],
            portfolio_space_kb_ids={"上海中医药大学附属岳阳医院": {"health": [hospital]}},
        )

        def handle(r):
            observed.append(json.loads(r.content))
            assert r.headers["Authorization"] == "Bearer medical-reader"
            return httpx.Response(
                200,
                json={
                    "evidence": [],
                    "trace_id": str(uuid4()),
                    "notes": ["low_relevance"],
                    "used_context_tokens": 0,
                    "decision": "reject",
                    "query_decisions": [{"decision": "reject", "covered": False}],
                },
            )

        client_type = httpx.AsyncClient
        monkeypatch.setattr(
            "meta_agent.domains.portfolio.httpx.AsyncClient",
            lambda **kw: client_type(transport=httpx.MockTransport(handle)),
        )
        ctx = SimpleNamespace(
            settings=settings,
            scope=SimpleNamespace(space_id=space, scene_id=None),
            record=SimpleNamespace(metrics={}),
        )
        adapter = PortfolioKnowledgeAdapter()
        task = SimpleNamespace(task_id="knowledge")
        result = await adapter.execute(
            task,
            {"query": "组合问题", "domain": "health", "subqueries": ["平衡评估", "步态康复"]},
            ctx,
        )
        assert result.code == "low_relevance" and observed[0]["kb_ids"] == [medical]
        assert observed[0]["queries"] == [{"q": "平衡评估"}, {"q": "步态康复"}]
        await adapter.execute(task, {"query": "补查", "domain": "health"}, ctx)
        with pytest.raises(DomainError) as rejected:
            await adapter.execute(task, {"query": "第三轮", "domain": "health"}, ctx)
        assert rejected.value.code == "knowledge_budget_exhausted" and len(observed) == 2
        assert ctx.record.metrics["knowledge_retrieval"][0]["space_id"] is None

    asyncio.run(check())


def test_scene_cache_tracks_the_published_registry_instead_of_the_bundled_snapshot(monkeypatch):
    async def check():
        settings = Settings(
            _env_file=None,
            portfolio_help_token="hospital-reader",
            portfolio_scene_catalog_ttl_seconds=1,
        )
        configure_scene_catalog(True)
        name = "新增的医院"
        scene = "新增诊疗室"

        def handler(r):
            assert r.url.path == "/v1/scene-catalogs"
            return httpx.Response(
                200,
                json={
                    name: {
                        "name": name,
                        "snapshot_id": str(uuid4()),
                        "source_version_id": str(uuid4()),
                        "source_locator": "guide",
                        "scenes": {scene: 2},
                    }
                },
            )

        client_type = httpx.AsyncClient
        monkeypatch.setattr(
            "meta_agent.domains.space_context.httpx.AsyncClient",
            lambda **kw: client_type(transport=httpx.MockTransport(handler)),
        )
        try:
            await refresh_scene_catalog(settings, required=True)
            assert current_space_context(name, scene)["floor"] == 2
            with pytest.raises(ValueError):
                current_space_context(name, "旧房间")
        finally:
            configure_scene_catalog(False)

    asyncio.run(check())


def test_rag_prompt_release_preserves_the_frozen_baselines():
    service = PromptService()
    assert len(service.releases()) == 3
    assert (
        service.bind("builtin-v1").bundle_hash
        == "a6ec735d80f1ff6cd8e3cb842e6c9aae8dd3156a829223ca146a5748db0d69f7"
    )
    assert service.bind("builtin-v3").definition("space.context").version == "v3"
