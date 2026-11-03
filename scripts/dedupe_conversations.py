"""
一次性脚本：清理会话历史里的重复消息（回放/重试导致），并给每条 turn 补递增 created_at。

背景：
    早期 save_conversation 把 turns 硬截断到 6 轮且不存每条消息的时间戳；会话回放/重试
    会重复追加相同问答。脚本按 (user, assistant) 去重（保留第一条），并按顺序补递增
    created_at，让 /v1/messages 的排序与游标分页恢复稳定。

用法（在 meta_agent 容器内执行）：
    docker exec meta_agent python scripts/dedupe_conversations.py           # dry-run，只报告
    docker exec meta_agent python scripts/dedupe_conversations.py --apply   # 实际写回
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os

from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg.conninfo import make_conninfo

# 密码经 META_AGENT__POSTGRES_DSN 注入；此默认值不携带任何真实凭据。
DEFAULT_DSN = "postgresql://meta_agent@postgres:5432/meta_agent"


def normalize_turns(turns: list, base_ts: float) -> tuple[list, int, int]:
    """去重 + 补 created_at。返回 (新turns, 原条数, 去掉的重复条数)。"""
    seen: set[tuple[str, str]] = set()
    out: list = []
    removed = 0
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        key = (str(turn.get("user", "")), str(turn.get("assistant", "")))
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        new = dict(turn)
        if not isinstance(new.get("created_at"), (int, float)):
            new["created_at"] = int(base_ts) + len(out)
        out.append(new)
    return out, len(turns), removed


async def main() -> None:
    parser = argparse.ArgumentParser(description="会话历史去重 + 补时间戳")
    parser.add_argument("--apply", action="store_true", help="实际写回；默认 dry-run 只打印。")
    args = parser.parse_args()

    dsn = os.environ.get("META_AGENT__POSTGRES_DSN", DEFAULT_DSN)
    conn = make_conninfo(dsn, connect_timeout=30)
    changed = 0
    async with AsyncPostgresStore.from_conn_string(conn) as store:
        items = await store.asearch(("metaagent-v1", "conversations"), limit=100000)
        for item in items:
            outer = item.value if isinstance(item.value, dict) else None
            if not outer or not isinstance(outer.get("value"), dict):
                continue
            value = outer["value"]
            turns = value.get("turns")
            if not isinstance(turns, list) or not turns:
                continue
            base_ts = 0.0
            updated = value.get("updated_at")
            if isinstance(updated, str):
                try:
                    base_ts = dt.datetime.fromisoformat(updated).timestamp()
                except ValueError:
                    base_ts = 0.0
            new_turns, original, removed = normalize_turns(turns, base_ts)
            if removed == 0:
                continue
            changed += 1
            tag = "APPLY" if args.apply else "dry-run"
            print(
                f"[{tag}] {item.key[:16]}… 原 {original} 条 -> {len(new_turns)} 条"
                f"（去重 {removed} 条）"
            )
            if args.apply:
                value["turns"] = new_turns
                await store.aput(
                    item.namespace,
                    item.key,
                    {"expires_at": outer.get("expires_at"), "value": value},
                )
    print(f"共 {changed} 个会话存在重复。", "已写回。" if args.apply else "（dry-run，未写回；加 --apply 实际执行）")


if __name__ == "__main__":
    asyncio.run(main())
