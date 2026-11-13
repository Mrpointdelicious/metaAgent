"""临时探针：测几个 query 在 MaxKB 各领域的命中情况。"""
import asyncio

from meta_agent.config import Settings
from meta_agent.integrations.knowledge import search


async def main():
    settings = Settings()
    source = settings.knowledge_sources[0]
    for query in ("左上角", "介绍左上角", "上海大学", "电脑端如何跳跃"):
        for domain in ("help", "product", "health"):
            payload = {
                "query": query,
                "domain": domain,
                "scope": {"space_id": "67ecd511-5532-405d-9b70-1cd1c0532eed"},
                "limits": {"max_context_tokens": 4000},
            }
            try:
                result = await search(source, payload, request_id="probe", max_bytes=8_000_000)
                n = len(result["sources"])
                first = result["sources"][0]["locator"] if n else ""
                print(f"[{domain}] {query} -> {result['status']} ({n}条) {first[:40]}")
            except Exception as e:
                print(f"[{domain}] {query} -> ERROR {type(e).__name__}: {str(e)[:120]}")


asyncio.run(main())
