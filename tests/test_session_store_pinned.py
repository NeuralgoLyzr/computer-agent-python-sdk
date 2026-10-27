"""A ``session_id`` keys the session store, so every process (sandbox) serving
a conversation reads and writes one transcript."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
import respx

from computeragent.engines.claude_agent import _pin_engine_session
from computeragent.session_stores import InMemorySessionStore, PinnedSessionStore
from computeragent.session_stores.http import HttpSessionStore
from test_session_store_wiring import OptionsCapturingEngine, build

BASE = "https://egress.example/computer-agent-api"
ENTRIES_URL = f"{BASE}/session-store/entries"
CAS = "agentos-6ae05f784be1b7463255fde4-1a2b3c4d5e6f"
CLI_UUID = "af60bf57-0000-4000-8000-000000000001"


@respx.mock
async def test_the_cli_uuid_never_reaches_the_http_store():
    load = respx.get(ENTRIES_URL).mock(return_value=httpx.Response(200, json={"entries": []}))
    post = respx.post(ENTRIES_URL).mock(return_value=httpx.Response(200, json={"appended": 1}))
    store = PinnedSessionStore(HttpSessionStore(base_url=BASE), CAS)

    await store.load({"project_key": "-workspace", "session_id": CLI_UUID})
    await store.append({"project_key": "-workspace", "session_id": CLI_UUID}, [{"uuid": "e1"}])
    sub = {"project_key": "-workspace", "session_id": CLI_UUID, "subpath": "subagents/agent-a"}
    await store.append(sub, [{"uuid": "e2"}])

    assert dict(load.calls[0].request.url.params) == {"sessionId": CAS, "projectKey": "-workspace"}
    bodies = [json.loads(c.request.content) for c in post.calls]
    assert [(b["sessionId"], b["projectKey"], b.get("subpath")) for b in bodies] == [
        (CAS, "-workspace", None),
        (CAS, "-workspace", "subagents/agent-a"),
    ]
    wire = str(load.calls[0].request.url) + "".join(c.request.content.decode() for c in post.calls)
    assert CLI_UUID not in wire


@pytest.mark.asyncio
@respx.mock
async def test_a_session_id_pins_the_store(agent_doc):
    engine = OptionsCapturingEngine()
    store = InMemorySessionStore()

    async with build(agent_doc, engine=engine, session_store=store, session_id=CAS) as agent:
        await agent.chat("hi")

    pinned = engine.options[0].session_store
    assert isinstance(pinned, PinnedSessionStore)
    assert (pinned.inner, pinned.pinned_session_id) == (store, CAS)


class CountingStore(InMemorySessionStore):
    def __init__(self) -> None:
        super().__init__()
        self.loads = 0

    async def load(self, key):
        self.loads += 1
        return await super().load(key)


def _engine_args(store, session_id=CAS, **options):
    opts = SimpleNamespace(**{"cwd": "/workspace", "resume": None, "session_id": None, **options})
    pinned = PinnedSessionStore(store, session_id) if session_id else store
    return opts, SimpleNamespace(session_store=pinned)


async def test_a_new_conversation_starts_under_its_derived_uuid():
    opts, ctx = _engine_args(InMemorySessionStore())

    await _pin_engine_session(opts, ctx)

    assert opts.resume is None
    assert uuid.UUID(opts.session_id).version == 5


async def test_a_stored_conversation_is_resumed_under_the_same_uuid():
    store = InMemorySessionStore()
    first, ctx = _engine_args(store)
    await _pin_engine_session(first, ctx)  # sandbox 1: a new conversation
    await ctx.session_store.append(
        {"project_key": "-workspace", "session_id": first.session_id}, [{"uuid": "e1"}]
    )

    second, ctx2 = _engine_args(store)  # sandbox 2, same conversation
    await _pin_engine_session(second, ctx2)

    assert (second.resume, second.session_id) == (first.session_id, None)
    other, ctx3 = _engine_args(store, session_id="agentos-other-1")
    await _pin_engine_session(other, ctx3)
    assert other.session_id not in (first.session_id, None)


async def test_a_known_resume_or_an_unpinned_store_is_left_alone():
    store = CountingStore()
    opts, ctx = _engine_args(store, resume="11111111-1111-4111-8111-111111111111")
    await _pin_engine_session(opts, ctx)
    unpinned, ctx2 = _engine_args(store, session_id=None)
    await _pin_engine_session(unpinned, ctx2)

    assert store.loads == 0
    assert opts.resume == "11111111-1111-4111-8111-111111111111" and opts.session_id is None
    assert (unpinned.resume, unpinned.session_id) == (None, None)
