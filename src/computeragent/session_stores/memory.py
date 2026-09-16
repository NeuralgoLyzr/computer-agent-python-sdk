"""``InMemorySessionStore`` — a process-local transcript mirror.

Implements the upstream ``SessionStore`` contract (``append`` / ``load`` over a
:class:`SessionKey`), so the engine receives it directly. Suitable for tests,
ephemeral runs, and resuming within one process; everything is lost on exit.

Port of the TS in-process default. For anything that must outlive the process,
use ``mongo``; for a sandbox that cannot hold a database credential, ``http``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..protocol_types import SessionKey, SessionStoreEntry


def slot_of(key: SessionKey) -> tuple[str, str, str]:
    """Flatten a :class:`SessionKey` into a dict key.

    ``subpath`` is part of the identity, not decoration: sub-agent transcripts
    arrive under the same session id with a ``subagents/agent-…`` subpath, and
    folding them into the main transcript would corrupt both.
    """
    return (key["project_key"], key["session_id"], key.get("subpath") or "")


class InMemorySessionStore:
    kind = "memory"

    def __init__(self) -> None:
        self._slots: dict[tuple[str, str, str], list[SessionStoreEntry]] = {}
        self._seen: dict[tuple[str, str, str], set[str]] = {}
        self._lock = asyncio.Lock()

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        """Mirror a batch. Entries carrying a ``uuid`` are deduped on it;
        entries without one (titles, tags, mode markers) always append."""
        if not entries:
            return
        slot = slot_of(key)
        async with self._lock:
            bucket = self._slots.setdefault(slot, [])
            seen = self._seen.setdefault(slot, set())
            for entry in entries:
                uuid = entry.get("uuid")
                if isinstance(uuid, str) and uuid:
                    if uuid in seen:
                        continue
                    seen.add(uuid)
                bucket.append(entry)

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        """``None`` for a key never written — the contract distinguishes that
        from an emptied one, and the SDK treats ``[]`` as a real empty
        transcript rather than a fresh session."""
        async with self._lock:
            bucket = self._slots.get(slot_of(key))
            return list(bucket) if bucket else None

    async def delete(self, key: SessionKey) -> None:
        slot = slot_of(key)
        async with self._lock:
            self._slots.pop(slot, None)
            self._seen.pop(slot, None)

    async def list_sessions(self, project_key: str) -> list[dict[str, Any]]:
        """Main transcripts for a project. Sub-agent subpaths are excluded —
        they are not sessions a caller can resume."""
        async with self._lock:
            return [
                {"session_id": session_id, "mtime": 0}
                for (project, session_id, subpath) in self._slots
                if project == project_key and not subpath
            ]


__all__ = ["InMemorySessionStore", "slot_of"]
