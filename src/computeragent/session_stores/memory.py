"""``InMemorySessionStore`` — process-local dict-backed
:class:`SessionStore`. Suitable for unit tests, ephemeral runs, and the
default for ``ComputerAgent`` when no store is explicitly configured.

Mongo / SQLite stores land in 0.3.0 alongside resumability.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from ..protocol_types import SessionStoreEntry


class InMemorySessionStore:
    kind = "memory"

    def __init__(self) -> None:
        self._slots: defaultdict[tuple[str, str], list[SessionStoreEntry]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def create(self, *, project_key: str, session_id: str) -> None:
        async with self._lock:
            self._slots.setdefault((project_key, session_id), [])

    async def load(
        self, *, project_key: str, session_id: str
    ) -> list[SessionStoreEntry]:
        async with self._lock:
            return list(self._slots.get((project_key, session_id), []))

    async def append(
        self,
        *,
        project_key: str,
        session_id: str,
        entry: SessionStoreEntry,
    ) -> None:
        async with self._lock:
            self._slots[(project_key, session_id)].append(entry)

    async def delete(self, *, project_key: str, session_id: str) -> None:
        async with self._lock:
            self._slots.pop((project_key, session_id), None)


__all__ = ["InMemorySessionStore"]
