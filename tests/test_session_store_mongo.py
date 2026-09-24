"""The Mongo session store.

Driven against a fake collection that applies the update operators the store
actually emits, so the tests pin the *update shape* — which is where the one
genuinely subtle bug in this port lives (see the ownership tests).

An `integration`-marked round-trip against a live server is at the bottom.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import pytest

from computeragent.session_stores.mongo import (
    DEFAULT_DATABASE,
    DEFAULT_TIMEOUT_MS,
    MongoSessionStore,
    mongo_session_store_builder,
)

KEY = {"project_key": "proj", "session_id": "sess-1"}


class FakeCollection:
    """Applies just the operators the store uses: $push/$each, $set, $setOnInsert."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    async def find_one(self, flt: dict[str, Any]) -> dict[str, Any] | None:
        return self.docs.get(flt["_id"]["$eq"])

    async def update_one(self, flt: dict[str, Any], update: dict[str, Any], upsert: bool = False):
        doc_id = flt["_id"]["$eq"]
        existing = self.docs.get(doc_id)
        if existing is None:
            if not upsert:
                return
            existing = dict(update.get("$setOnInsert") or {})
            existing.setdefault("entries", [])
            self.docs[doc_id] = existing
        existing.update(update.get("$set") or {})
        for field, spec in (update.get("$push") or {}).items():
            existing.setdefault(field, []).extend(spec["$each"])

    async def delete_one(self, flt: dict[str, Any]) -> None:
        self.docs.pop(flt["_id"]["$eq"], None)

    def find(self, flt: dict[str, Any]):
        matched = [
            d | {"_id": k}
            for k, d in self.docs.items()
            if d.get("projectKey") == flt["projectKey"]["$eq"]
        ]

        class _Cursor:
            def __init__(self, rows):
                self.rows = rows

            def sort(self, *_a, **_k):
                return self

            def limit(self, _n):
                return self

            def __aiter__(self):
                async def gen():
                    for r in self.rows:
                        yield r

                return gen()

        return _Cursor(matched)


class FakeDb:
    def __init__(self, coll: FakeCollection) -> None:
        self._coll = coll

    def __getitem__(self, _name: str) -> FakeCollection:
        return self._coll


class FakeClient:
    """`client[database][collection]` — two levels, as pymongo does."""

    def __init__(self) -> None:
        self.coll = FakeCollection()
        self.closed = False

    def __getitem__(self, _name: str) -> FakeDb:
        return FakeDb(self.coll)

    def close(self) -> None:
        self.closed = True


def store(**kw) -> tuple[MongoSessionStore, FakeCollection]:
    client = FakeClient()
    params: dict[str, Any] = {"url": "mongodb://x", "database": "db", "client": client}
    params.update(kw)
    s = MongoSessionStore(**params)
    return s, client.coll


def entry(uuid: str | None = None):
    e: dict[str, Any] = {"type": "user"}
    if uuid:
        e["uuid"] = uuid
    return e


# ── construction ─────────────────────────────────────────────────────────────


def test_warns_once_when_no_database_is_configured(caplog):
    """The default is NOT the database AgentOS reads, so transcripts would land
    where nothing looks — silence here is the expensive option."""
    with caplog.at_level("WARNING"):
        MongoSessionStore(url="mongodb://x", client=FakeClient())

    warnings = [r for r in caplog.records if DEFAULT_DATABASE in r.getMessage()]
    assert len(warnings) == 1


def test_no_warning_when_a_database_is_given(caplog):
    with caplog.at_level("WARNING"):
        MongoSessionStore(url="mongodb://x", database="db", client=FakeClient())

    assert not [r for r in caplog.records if DEFAULT_DATABASE in r.getMessage()]


def test_url_is_required_without_an_injected_client():
    with pytest.raises(ValueError, match="url is required"):
        MongoSessionStore(url="")


def test_timeouts_are_clamped_to_ten_seconds(monkeypatch):
    """The driver default is 30s, which turns an unreachable cluster into half
    a minute of dead air before the session fails."""
    captured: dict[str, Any] = {}

    class _Client:
        def __init__(self, url, **opts):
            captured.update(opts)

    import pymongo

    monkeypatch.setattr(pymongo, "AsyncMongoClient", _Client)
    MongoSessionStore(url="mongodb://x", database="db")

    assert captured["serverSelectionTimeoutMS"] == DEFAULT_TIMEOUT_MS
    assert captured["connectTimeoutMS"] == DEFAULT_TIMEOUT_MS


# ── append / load ────────────────────────────────────────────────────────────


async def test_round_trip():
    s, _ = store()
    await s.append(KEY, [entry("a"), entry("b")])

    assert [e["uuid"] for e in await s.load(KEY)] == ["a", "b"]


async def test_load_of_an_unwritten_key_is_none():
    s, _ = store()
    assert await s.load(KEY) is None


async def test_load_of_a_document_with_no_entries_is_none():
    """`None`, not `[]` — the SDK starts a fresh session only for 'never
    written', and a row the telemetry projection created has no entries yet."""
    s, coll = store()
    coll.docs["sess-1"] = {"projectKey": "proj", "entries": []}

    assert await s.load(KEY) is None


async def test_uuid_dedup_across_repeated_batches():
    s, _ = store()
    await s.append(KEY, [entry("a"), entry("b")])
    await s.append(KEY, [entry("b"), entry("c")])

    assert [e["uuid"] for e in await s.load(KEY)] == ["a", "b", "c"]


async def test_entries_without_a_uuid_always_append():
    s, _ = store()
    await s.append(KEY, [entry(), entry()])

    assert len(await s.load(KEY)) == 2


async def test_a_fully_duplicate_batch_writes_nothing():
    s, coll = store()
    await s.append(KEY, [entry("a")])
    before = list(coll.docs["sess-1"]["entries"])

    await s.append(KEY, [entry("a")])

    assert coll.docs["sess-1"]["entries"] == before


async def test_append_order_is_preserved():
    s, _ = store()
    for uuid in "abcde":
        await s.append(KEY, [entry(uuid)])

    assert [e["uuid"] for e in await s.load(KEY)] == list("abcde")


async def test_subpath_is_a_separate_document():
    s, _ = store()
    await s.append(KEY, [entry("main")])
    await s.append({**KEY, "subpath": "subagents/agent-1"}, [entry("child")])

    assert [e["uuid"] for e in await s.load(KEY)] == ["main"]
    assert [e["uuid"] for e in await s.load({**KEY, "subpath": "subagents/agent-1"})] == ["child"]


async def test_delete_removes_the_document():
    s, _ = store()
    await s.append(KEY, [entry("a")])

    await s.delete(KEY)

    assert await s.load(KEY) is None


# ── ownership: the $setOnInsert trap ─────────────────────────────────────────


async def test_ownership_is_stamped_on_a_pre_created_row():
    """THE regression test for this port.

    With telemetry ingest enabled the server's projection upserts the sessions
    row on `session_started`, before any transcript entry exists. An
    implementation using `$setOnInsert` passes a naive insert-then-append test
    and fails this one: the document already exists, the operator never fires,
    the row stays unowned — and since an unowned row matches everyone, tenancy
    enforcement silently becomes a no-op.
    """
    s, coll = store(owner_user="u1", owner_group="g1")
    coll.docs["sess-1"] = {"projectKey": "proj", "entries": []}  # pre-created, unowned

    await s.append(KEY, [entry("a")])

    assert coll.docs["sess-1"]["ownerUser"] == "u1"
    assert coll.docs["sess-1"]["ownerGroup"] == "g1"


async def test_ownership_is_stamped_on_a_fresh_insert_too():
    s, coll = store(owner_user="u1")
    await s.append(KEY, [entry("a")])

    assert coll.docs["sess-1"]["ownerUser"] == "u1"


async def test_an_existing_owner_is_never_overwritten():
    """Trust on first use: the first authenticated writer claims the row."""
    s, coll = store(owner_user="u2")
    coll.docs["sess-1"] = {"projectKey": "proj", "entries": [], "ownerUser": "u1"}

    await s.append(KEY, [entry("a")])

    assert coll.docs["sess-1"]["ownerUser"] == "u1"


async def test_an_unknown_owner_is_left_unstamped():
    """The in-process paths carry no identity. Writing a null would turn
    UNKNOWN ownership into an explicit 'owned by nobody', which reads
    differently to a reader enforcing isolation."""
    s, coll = store()
    await s.append(KEY, [entry("a")])

    assert "ownerUser" not in coll.docs["sess-1"]


# ── listing + lifecycle ──────────────────────────────────────────────────────


async def test_list_sessions_reports_ids_and_mtimes():
    s, coll = store()
    await s.append(KEY, [entry("a")])
    coll.docs["sess-1"]["updatedAt"] = datetime(2026, 1, 1, tzinfo=timezone.utc)

    listed = await s.list_sessions("proj")

    assert listed[0]["session_id"] == "sess-1"
    assert listed[0]["mtime"] > 0


async def test_close_closes_the_client():
    client = FakeClient()
    s = MongoSessionStore(url="mongodb://x", database="db", client=client)

    await s.close()

    assert client.closed


# ── builder ──────────────────────────────────────────────────────────────────


def test_builder_merges_per_call_options_over_defaults():
    build = mongo_session_store_builder(url="mongodb://x", database="base", client=FakeClient())

    built = build({"database": "tenant_a"})

    assert built._database == "tenant_a"


def test_registry_exposes_the_kind():
    from computeragent.session_stores import _REGISTRY

    assert "mongo" in _REGISTRY


# ── live ─────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("CA_TEST_MONGO_URL"), reason="set CA_TEST_MONGO_URL to run")
async def test_live_round_trip():
    s = MongoSessionStore(
        url=os.environ["CA_TEST_MONGO_URL"],
        database=os.environ.get("CA_TEST_MONGO_DB", "computeragent_test"),
    )
    key = {"project_key": "pytest", "session_id": f"sess-{os.getpid()}"}
    try:
        await s.append(key, [entry("a"), entry("b")])
        await s.append(key, [entry("b")])  # dedup

        assert [e["uuid"] for e in await s.load(key)] == ["a", "b"]
    finally:
        await s.delete(key)
        await s.close()
