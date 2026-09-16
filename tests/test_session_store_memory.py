"""The in-process store, and the contract every store shares.

These assertions are the contract, not implementation detail: the SDK
distinguishes "never written" from "written and empty", and dedupes on ``uuid``
only where one is present.
"""

from __future__ import annotations

from computeragent.session_stores import InMemorySessionStore

KEY = {"project_key": "proj", "session_id": "sess-1"}


def entry(uuid: str | None = None, **extra):
    e = {"type": "user", **extra}
    if uuid:
        e["uuid"] = uuid
    return e


async def test_load_of_an_unwritten_key_is_none():
    assert await InMemorySessionStore().load(KEY) is None


async def test_round_trip():
    store = InMemorySessionStore()
    await store.append(KEY, [entry("a"), entry("b")])

    loaded = await store.load(KEY)

    assert loaded is not None
    assert [e["uuid"] for e in loaded] == ["a", "b"]


async def test_uuid_dedup_across_repeated_batches():
    store = InMemorySessionStore()
    await store.append(KEY, [entry("a"), entry("b")])
    await store.append(KEY, [entry("b"), entry("c")])

    loaded = await store.load(KEY)

    assert [e["uuid"] for e in loaded] == ["a", "b", "c"]


async def test_entries_without_a_uuid_always_append():
    """Titles, tags and mode markers carry no uuid; deduping them would drop
    content that is legitimately repeated."""
    store = InMemorySessionStore()
    await store.append(KEY, [entry(), entry()])

    assert len(await store.load(KEY)) == 2


async def test_an_empty_batch_is_a_no_op():
    store = InMemorySessionStore()
    await store.append(KEY, [])

    assert await store.load(KEY) is None


async def test_subpath_is_a_separate_transcript():
    """Sub-agent transcripts share the session id; folding them into the main
    one would corrupt both."""
    store = InMemorySessionStore()
    sub = {**KEY, "subpath": "subagents/agent-1"}

    await store.append(KEY, [entry("main")])
    await store.append(sub, [entry("child")])

    assert [e["uuid"] for e in await store.load(KEY)] == ["main"]
    assert [e["uuid"] for e in await store.load(sub)] == ["child"]


async def test_sessions_are_isolated_by_project():
    store = InMemorySessionStore()
    await store.append(KEY, [entry("a")])

    assert await store.load({**KEY, "project_key": "other"}) is None


async def test_delete_removes_the_transcript():
    store = InMemorySessionStore()
    await store.append(KEY, [entry("a")])

    await store.delete(KEY)

    assert await store.load(KEY) is None


async def test_delete_clears_the_dedup_memory_too():
    """Otherwise a re-appended entry would be silently swallowed after a
    delete, leaving the transcript permanently short."""
    store = InMemorySessionStore()
    await store.append(KEY, [entry("a")])
    await store.delete(KEY)

    await store.append(KEY, [entry("a")])

    assert len(await store.load(KEY)) == 1


async def test_list_sessions_excludes_subagent_transcripts():
    store = InMemorySessionStore()
    await store.append(KEY, [entry("a")])
    await store.append({**KEY, "subpath": "subagents/agent-1"}, [entry("b")])

    listed = await store.list_sessions("proj")

    assert [s["session_id"] for s in listed] == ["sess-1"]


def test_satisfies_the_duck_typed_contract():
    from computeragent.session_stores import is_session_store

    assert is_session_store(InMemorySessionStore())
    assert not is_session_store(object())
