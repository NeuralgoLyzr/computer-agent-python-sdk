"""The session store has to reach the engine, not just the coordinator.

Before this wiring existed the axis was inert: a store could be configured, the
coordinator would hold it, and nothing would ever read or write it. These tests
are what makes that failure impossible to reintroduce quietly.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from computeragent.harness import ComputerAgent
from computeragent.protocol_types import EngineCapabilities, EngineContext, EngineEvent
from computeragent.session_stores import InMemorySessionStore

AGENT_ID = "6aa93f1b3817a13d3103d2c1"
BASE = "http://localhost:8788/agentos/api"
URL = f"{BASE}/v1/agents/{AGENT_ID}"


class OptionsCapturingEngine:
    """Runs the real `_build_options`, so what the SDK would receive is what
    gets asserted — not a hand-built approximation."""

    name = "claude-agent-sdk"
    capabilities = EngineCapabilities(
        streaming_input=True,
        partial_messages=False,
        permission_callback=True,
        sessions=True,
        budget=False,
    )

    def __init__(self) -> None:
        self.options: list = []

    async def start_session(self, ctx: EngineContext):
        from computeragent.engines.claude_agent import _build_options

        self.options.append(_build_options(ctx))
        async for _ in ctx.user_message_queue:
            break
        yield EngineEvent(kind="sdk_message", payload={"type": "assistant"})


def build(agent_doc, **kwargs) -> ComputerAgent:
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))
    params: dict = {
        "agent_id": AGENT_ID,
        "base_url": BASE,
        "api_key": "ags_test",
        "engine": "claude-agent-sdk",
        "llm_url": "http://egress.internal/llm",
    }
    params.update(kwargs)
    return ComputerAgent(**params)


@pytest.mark.asyncio
@respx.mock
async def test_the_store_reaches_the_engines_options(agent_doc, tmp_path):
    """The assertion that would have caught the inert axis."""
    engine = OptionsCapturingEngine()
    store = InMemorySessionStore()

    async with build(agent_doc, engine=engine, session_store=store) as agent:
        await agent.chat("hi")

    assert engine.options[0].session_store is store


@pytest.mark.asyncio
@respx.mock
async def test_no_store_leaves_the_option_unset(agent_doc, tmp_path):
    engine = OptionsCapturingEngine()

    async with build(agent_doc, engine=engine) as agent:
        await agent.chat("hi")

    assert engine.options[0].session_store is None


@pytest.mark.asyncio
@respx.mock
async def test_the_flush_mode_the_sdk_defaults_to_is_preserved(agent_doc, tmp_path):
    engine = OptionsCapturingEngine()

    async with build(agent_doc, engine=engine, session_store=InMemorySessionStore()) as agent:
        await agent.chat("hi")

    assert engine.options[0].session_store_flush == "batched"


@pytest.mark.asyncio
@respx.mock
async def test_a_caller_supplied_flush_mode_wins(agent_doc, tmp_path):
    engine = OptionsCapturingEngine()

    async with build(
        agent_doc,
        engine=engine,
        session_store=InMemorySessionStore(),
        options={"session_store_flush": "immediate"},
    ) as agent:
        await agent.chat("hi")

    assert engine.options[0].session_store_flush == "immediate"


@pytest.mark.asyncio
@respx.mock
async def test_the_harness_does_not_replay_the_store_itself(agent_doc, tmp_path):
    """The SDK materialises prior entries before spawning the subprocess. If the
    coordinator replayed them too, every resumed turn would see the transcript
    twice."""
    loads: list = []

    class CountingStore(InMemorySessionStore):
        async def load(self, key):
            loads.append(key)
            return await super().load(key)

    engine = OptionsCapturingEngine()
    async with build(agent_doc, engine=engine, session_store=CountingStore()) as agent:
        await agent.chat("hi")

    assert loads == []


@pytest.mark.asyncio
@respx.mock
async def test_a_duck_typed_store_is_accepted(agent_doc, tmp_path):
    """Upstream's Protocol is deliberately not runtime-checkable: only `append`
    and `load` are required and call sites probe for the rest."""

    class Minimal:
        def __init__(self):
            self.appended = []

        async def append(self, key, entries):
            self.appended.append((key, entries))

        async def load(self, key):
            return None

    store = Minimal()
    engine = OptionsCapturingEngine()

    async with build(agent_doc, engine=engine, session_store=store) as agent:
        await agent.chat("hi")

    assert engine.options[0].session_store is store


@pytest.mark.asyncio
@respx.mock
async def test_an_object_that_is_not_a_store_is_refused(agent_doc, tmp_path):
    from computeragent.harness.errors import UnknownSessionStoreError

    with pytest.raises((UnknownSessionStoreError, TypeError, AttributeError)):
        build(agent_doc, session_store=object())
