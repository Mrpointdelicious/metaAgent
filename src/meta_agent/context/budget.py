"""
创建日期：2026-09-08
文件功能：统一模型输入预算、调用次数和跨目标事实覆盖分配。
"""

import asyncio
import json
import math
import re
from typing import Any

from meta_agent.contracts import DomainError, FactView


def estimate_tokens(value: Any) -> int:
    """无供应商tokenizer时保守估计；运行指标明确标注estimated。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    non_ascii = sum(ord(char) > 127 for char in text)
    return math.ceil((len(text) - non_ascii) / 3) + non_ascii * 2 + 16


class LLMBudget:
    def __init__(self, maximum: int = 4) -> None:
        self.maximum = maximum
        self.calls = 0
        self.usage: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        async with self._lock:
            if self.calls >= self.maximum:
                raise DomainError("llm_budget", "本轮模型调用预算已用完。", outcome="unavailable")
            self.calls += 1


class ContextSelector:
    """先保留完整必需事实，再按目标轮流填充相关事实。"""

    def select(
        self, query: str, groups: dict[str, list[FactView]], budget: int, overhead: Any = None
    ) -> dict[str, list[FactView]]:
        selected: dict[str, list[FactView]] = {key: [] for key in groups}
        remaining = budget - estimate_tokens({"query": query, "instructions": overhead})
        candidates: dict[str, list[FactView]] = {}
        terms = set(re.findall(r"[A-Za-z_]+|[\u4e00-\u9fff]", query.lower()))
        for key, group in groups.items():
            unique = {view.fact.fact_id: view for view in group}.values()
            pool = []
            for view in unique:
                if view.required:
                    selected[key].append(view)
                    remaining -= estimate_tokens(view.model_dump(mode="json"))
                else:
                    pool.append(view)
            candidates[key] = sorted(
                pool,
                key=lambda view: (
                    -sum(
                        term in f"{view.label} {view.topic} {view.fact.semantic_key}".lower()
                        for term in terms
                    )
                ),
            )
        if remaining < 0 or any(len(group) > 100 for group in selected.values()):
            raise DomainError(
                "context_required_overflow",
                "必要事实超出本轮预算，请缩小查询范围。",
                outcome="clarification",
            )
        while any(candidates.values()):
            for key, pool in candidates.items():
                if not pool:
                    continue
                view = pool.pop(0)
                cost = estimate_tokens(view.model_dump(mode="json"))
                if cost <= remaining and len(selected[key]) < 100:
                    selected[key].append(view)
                    remaining -= cost
        return selected
