from __future__ import annotations

import copy
import threading
from typing import Any

from agents.memory import SessionSettings

from config import Settings, get_settings


class InMemoryAgentSession:
    """Small SDK-compatible session used by tests and local debug runs."""

    def __init__(self, session_id: str, *, session_settings: SessionSettings | None = None):
        self.session_id = session_id
        self.session_settings = session_settings or SessionSettings()
        self._items: list[Any] = []
        self._lock = threading.RLock()

    async def get_items(self, limit: int | None = None) -> list[Any]:
        resolved_limit = limit if limit is not None else self.session_settings.limit
        with self._lock:
            if resolved_limit is None:
                items = self._items
            elif resolved_limit <= 0:
                items = []
            else:
                items = self._items[-resolved_limit:]
            return copy.deepcopy(items)

    async def add_items(self, items: list[Any]) -> None:
        if not items:
            return
        with self._lock:
            self._items.extend(copy.deepcopy(items))

    async def pop_item(self) -> Any | None:
        with self._lock:
            if not self._items:
                return None
            return copy.deepcopy(self._items.pop())

    async def clear_session(self) -> None:
        with self._lock:
            self._items.clear()


class AgentSessionManager:
    """Creates and caches OpenAI Agents SDK sessions keyed by thread ID.

    The caller should pass `conversation_id` when available so raw history is
    isolated by conversation thread. `session_id` remains a fallback for older
    callers that do not yet provide a conversation identifier.
    The production backend is the Agents SDK RedisSession. The in-memory backend
    exists for unit tests and local debugging where Redis is intentionally absent.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._sessions: dict[str, Any] = {}
        self._lock = threading.RLock()

    def get_or_create_session(self, session_id: str) -> Any:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            raise ValueError("thread_id is required for SDK session history")

        with self._lock:
            existing = self._sessions.get(normalized_session_id)
            if existing is not None:
                return existing

            session = self._create_session(normalized_session_id)
            self._sessions[normalized_session_id] = session
            return session

    def _create_session(self, session_id: str) -> Any:
        session_settings = SessionSettings(limit=self.settings.agent_session_history_limit)
        if self.settings.agent_session_backend == "memory":
            return InMemoryAgentSession(session_id, session_settings=session_settings)

        redis_url = self.settings.agent_session_redis_url
        if not redis_url:
            raise RuntimeError("agents_sdk_runtime.session_store.redis_url_missing")

        try:
            from agents.extensions.memory import RedisSession
        except ImportError as exc:
            raise RuntimeError("agents_sdk_runtime.session_store.redis_dependency_missing") from exc

        return RedisSession.from_url(
            session_id=session_id,
            url=redis_url,
            key_prefix=self.settings.agent_session_redis_key_prefix,
            ttl=self.settings.agent_session_ttl_seconds,
            session_settings=session_settings,
        )

