"""Coverage for the in-memory session store."""

from __future__ import annotations

import asyncio

import pytest

from computeragent.protocol_types import SessionStoreEntry
from computeragent.session_stores import (
    InMemorySessionStore,
    resolve_session_store,
)


@pytest.mark.asyncio
async def test_append_load_preserves_order():
    store = InMemorySessionStore()
    await store.create(project_key="p", session_id="s")
    for i in range(5):
        await store.append(
            project_key="p",
            session_id="s",
            entry=SessionStoreEntry(role="user", content=f"msg-{i}", timestamp=float(i)),
        )

    entries = await store.load(project_key="p", session_id="s")
    assert [e.content for e in entries] == [f"msg-{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_delete_clears_slot():
    store = InMemorySessionStore()
    await store.create(project_key="p", session_id="s")
    await store.append(
        project_key="p",
        session_id="s",
        entry=SessionStoreEntry(role="user", content="x", timestamp=1.0),
    )
    await store.delete(project_key="p", session_id="s")
    assert await store.load(project_key="p", session_id="s") == []


@pytest.mark.asyncio
async def test_session_isolation_by_key():
    """Two sessions under the same project — appends to one do not bleed."""
    store = InMemorySessionStore()
    await store.append(
        project_key="p",
        session_id="s1",
        entry=SessionStoreEntry(role="user", content="hello", timestamp=1.0),
    )
    await store.append(
        project_key="p",
        session_id="s2",
        entry=SessionStoreEntry(role="user", content="world", timestamp=2.0),
    )
    assert len(await store.load(project_key="p", session_id="s1")) == 1
    assert len(await store.load(project_key="p", session_id="s2")) == 1


@pytest.mark.asyncio
async def test_concurrent_appends_preserve_all_entries():
    """Async-Lock guard means even N concurrent producers all land."""
    store = InMemorySessionStore()
    n = 50

    async def _writer(i: int) -> None:
        await store.append(
            project_key="p",
            session_id="s",
            entry=SessionStoreEntry(role="user", content=f"x-{i}", timestamp=float(i)),
        )

    await asyncio.gather(*(_writer(i) for i in range(n)))
    assert len(await store.load(project_key="p", session_id="s")) == n


@pytest.mark.asyncio
async def test_registry_resolves_memory():
    store = resolve_session_store("memory")
    assert store.kind == "memory"
    # The registry returns a module-level singleton — separate calls
    # see the same store. Verified by writing once and reading from
    # a fresh resolve.
    await store.append(
        project_key="p",
        session_id="registry-roundtrip",
        entry=SessionStoreEntry(role="user", content="probe", timestamp=1.0),
    )
    again = resolve_session_store("memory")
    assert again is store
    entries = await again.load(project_key="p", session_id="registry-roundtrip")
    assert len(entries) == 1
    # Cleanup so this test is idempotent.
    await store.delete(project_key="p", session_id="registry-roundtrip")
