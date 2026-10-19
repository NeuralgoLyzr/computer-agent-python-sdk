"""Constructing a :class:`ComputerAgent` — what it resolves and how it wires up.

No real subprocess and no network: AgentOS is mocked with ``respx`` and the
engine is a fake that yields a canned message sequence. What is under test is
the translation from an agent document to a booted coordinator.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx

from computeragent.harness import ComputerAgent
from computeragent.harness.agentos_agent import AgentResolveError
from computeragent.harness.errors import UnknownEngineError
from computeragent.harness.gateway_envs import GatewayConfigError
from computeragent.protocol_types import EngineCapabilities, EngineContext, EngineEvent

AGENT_ID = "6aa93f1b3817a13d3103d2c1"
BASE = "http://localhost:8788/agentos/api"
URL = f"{BASE}/v1/agents/{AGENT_ID}"
KEY = "ags_testkey"
PROXY = "http://127.0.0.1:8786"


class FakeEngine:
    """Records the context it was started with, yields one assistant message."""

    name = "claude-agent-sdk"
    capabilities = EngineCapabilities(
        streaming_input=True,
        partial_messages=False,
        permission_callback=True,
        sessions=True,
        budget=False,
    )

    def __init__(self) -> None:
        self.contexts: list[EngineContext] = []

    async def start_session(self, ctx: EngineContext) -> AsyncIterator[EngineEvent]:
        self.contexts.append(ctx)
        # Drain one user message so the queue's sentinel is consumed.
        async for _ in ctx.user_message_queue:
            break
        yield EngineEvent(
            kind="sdk_message", payload={"type": "assistant", "message": {"content": "ok"}}
        )


def build(agent_doc, **kwargs) -> ComputerAgent:
    """Construct against a mocked AgentOS, with sensible defaults."""
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))
    params: dict = {
        "agent_id": AGENT_ID,
        "base_url": BASE,
        "api_key": KEY,
        "llm_proxy_url": PROXY,
        "source": Path.cwd(),
        "engine": FakeEngine(),
    }
    params.update(kwargs)
    return ComputerAgent(**params)


# ── resolution ───────────────────────────────────────────────────────────────


@respx.mock
def test_exposes_the_resolved_agent(agent_doc):
    agent = build(agent_doc)

    assert agent.agent.agent_id == AGENT_ID
    assert agent.agent.model == "6aa93f1a75ce74c4e6772b20"
    assert agent.agent.has_native_gateway


@respx.mock
def test_resolution_failure_surfaces_at_construction(agent_doc):
    """Better a failed constructor than a half-built agent that dies on chat."""
    respx.get(URL).mock(return_value=httpx.Response(404, json={"error": {"code": "NOT_FOUND"}}))

    with pytest.raises(AgentResolveError):
        ComputerAgent(
            agent_id=AGENT_ID,
            base_url=BASE,
            api_key=KEY,
            source=Path.cwd(),
            engine=FakeEngine(),
        )


@respx.mock
def test_missing_proxy_is_refused_at_construction(agent_doc):
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    with pytest.raises(GatewayConfigError, match="llm_proxy_url"):
        ComputerAgent(
            agent_id=AGENT_ID, base_url=BASE, api_key=KEY, source=Path.cwd(), engine=FakeEngine()
        )


# ── engine selection ─────────────────────────────────────────────────────────


def test_engine_is_required():
    """Not inferred from the agent document: `engine` also selects which
    gateway-env branch is built, so a document change must not be able to
    re-shape ANTHROPIC_BASE_URL under a caller who never asked."""
    with pytest.raises(TypeError, match="engine"):
        ComputerAgent(  # type: ignore[call-arg]
            agent_id=AGENT_ID, base_url=BASE, api_key=KEY, llm_proxy_url=PROXY
        )


@respx.mock
def test_a_named_engine_resolves_to_the_registered_driver(agent_doc):
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    agent = ComputerAgent(
        agent_id=AGENT_ID,
        base_url=BASE,
        api_key=KEY,
        llm_proxy_url=PROXY,
        source=Path.cwd(),
        engine="claude-agent-sdk",
    )

    assert agent._coordinator._engine.name == "claude-agent-sdk"  # type: ignore[attr-defined]


@respx.mock
def test_an_unregistered_engine_name_is_refused(agent_doc):
    """The replacement for the old harness-runnability check: the caller names
    the engine, so the caller is the one told what is registered."""
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    with pytest.raises(UnknownEngineError, match="claude-agent-sdk"):
        ComputerAgent(
            agent_id=AGENT_ID,
            base_url=BASE,
            api_key=KEY,
            llm_proxy_url=PROXY,
            source=Path.cwd(),
            engine="langgraph",
        )


@respx.mock
def test_an_engine_that_differs_from_the_harness_is_logged(agent_doc, caplog):
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    with caplog.at_level("WARNING", logger="computeragent.harness"):
        ComputerAgent(
            agent_id=AGENT_ID, base_url=BASE, api_key=KEY, source=Path.cwd(), engine="gitagent"
        )

    assert any("overrides agent" in r.getMessage() for r in caplog.records)


@respx.mock
def test_engine_can_be_named(agent_doc):
    """A gitagent engine needs no proxy, so this also pins that the env branch
    follows the ENGINE rather than the agent's declared harness."""
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    agent = ComputerAgent(
        agent_id=AGENT_ID, base_url=BASE, api_key=KEY, source=Path.cwd(), engine="gitagent"
    )

    assert agent._coordinator._engine.name == "gitagent"  # type: ignore[attr-defined]


@respx.mock
def test_engine_can_be_an_instance(agent_doc):
    engine = FakeEngine()
    agent = build(agent_doc, engine=engine)

    assert agent._coordinator._engine is engine  # type: ignore[attr-defined]


# ── source ───────────────────────────────────────────────────────────────────


@respx.mock
def test_nothing_is_cloned_by_default(agent_doc):
    """Without clone_source the agent's own git source is not read."""
    agent = build(agent_doc, source=None)

    assert agent._coordinator._source == Path.cwd()  # type: ignore[attr-defined]


@respx.mock
def test_cloning_uses_the_agents_own_source(agent_doc):
    agent = build(agent_doc, source=None, clone_source=True)

    assert agent._coordinator._source == agent_doc["source"]  # type: ignore[attr-defined]


@respx.mock
def test_caller_source_overrides_the_agents(agent_doc, tmp_path):
    agent = build(agent_doc, source=tmp_path, clone_source=True)

    assert agent._coordinator._source == tmp_path  # type: ignore[attr-defined]


@respx.mock
def test_cloning_with_no_source_anywhere_is_refused(agent_doc):
    agent_doc["source"] = None
    agent_doc["sourceUrl"] = None

    with pytest.raises(AgentResolveError, match="no source"):
        build(agent_doc, source=None, clone_source=True)


@respx.mock
def test_without_cloning_the_current_directory_is_the_workspace(agent_doc):
    """The agent's own git source is not read, so nothing is cloned."""
    agent = build(agent_doc, source=None, clone_source=False)

    assert agent._coordinator._source == Path.cwd()  # type: ignore[attr-defined]


@respx.mock
def test_without_cloning_a_local_source_is_used(agent_doc, tmp_path):
    agent = build(agent_doc, source=str(tmp_path), clone_source=False)

    assert agent._coordinator._source == tmp_path  # type: ignore[attr-defined]


@respx.mock
def test_without_cloning_an_agent_with_no_source_runs(agent_doc):
    agent_doc["source"] = None
    agent_doc["sourceUrl"] = None

    agent = build(agent_doc, source=None, clone_source=False)

    assert agent._coordinator._source == Path.cwd()  # type: ignore[attr-defined]


@respx.mock
def test_without_cloning_a_git_url_is_refused(agent_doc):
    with pytest.raises(AgentResolveError, match="git URL"):
        build(agent_doc, source="https://github.com/acme/agent.git", clone_source=False)


@respx.mock
def test_without_cloning_an_inline_manifest_is_refused(agent_doc):
    with pytest.raises(AgentResolveError, match="inline manifest"):
        build(agent_doc, source={"files": {}}, clone_source=False)


@pytest.mark.asyncio
@respx.mock
async def test_without_cloning_the_substrate_clones_nothing(agent_doc, tmp_path, monkeypatch):
    """End to end through boot: the substrate is handed a Path and never runs git."""
    import computeragent.substrates.local as local

    async def no_clone(*_a, **_k):
        raise AssertionError("git clone was attempted")

    monkeypatch.setattr(local, "_git_clone", no_clone)
    engine = FakeEngine()

    async with build(agent_doc, source=tmp_path, clone_source=False, engine=engine) as agent:
        await agent.chat("hi")

    assert engine.contexts[0].workspace_path == tmp_path


# ── what the coordinator is handed ───────────────────────────────────────────


@respx.mock
def test_gateway_envs_reach_the_coordinator(agent_doc):
    agent = build(agent_doc)
    envs = agent._coordinator._envs  # type: ignore[attr-defined]

    assert envs["ANTHROPIC_API_KEY"] == KEY
    assert envs["LYZR_API_KEY"] == KEY
    assert envs["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] == "1"
    assert envs["ANTHROPIC_BASE_URL"].startswith(f"{PROXY}/up/")


@respx.mock
def test_caller_envs_are_preserved_alongside_gateway_envs(agent_doc):
    agent = build(agent_doc, envs={"MY_VAR": "kept"})

    assert agent._coordinator._envs["MY_VAR"] == "kept"  # type: ignore[attr-defined]


@respx.mock
def test_options_overlay_carries_dispatch_defaults_and_caller_options(agent_doc):
    agent = build(agent_doc, options={"system_prompt": "You are terse."})
    overlay = agent._coordinator._options_overlay  # type: ignore[attr-defined]

    assert overlay["permission_mode"] == "bypassPermissions"
    assert overlay["setting_sources"] == ["project"]
    assert overlay["model"] == "6aa93f1a75ce74c4e6772b20"
    assert overlay["system_prompt"] == "You are terse."


@respx.mock
def test_agent_id_is_stamped_for_telemetry(agent_doc):
    agent = build(agent_doc)
    assert agent._coordinator._agent_id == AGENT_ID  # type: ignore[attr-defined]


@respx.mock
def test_agent_name_defaults_to_the_resolved_name(agent_doc):
    agent = build(agent_doc)
    assert agent._coordinator._agent_name == agent_doc["name"]  # type: ignore[attr-defined]


@respx.mock
def test_agent_name_can_be_overridden(agent_doc):
    agent = build(agent_doc, agent_name="my-label")
    assert agent._coordinator._agent_name == "my-label"  # type: ignore[attr-defined]


# ── session store ────────────────────────────────────────────────────────────


@respx.mock
def test_session_store_by_name(agent_doc):
    agent = build(agent_doc, session_store="memory")
    assert agent._coordinator._session_store is not None  # type: ignore[attr-defined]


@respx.mock
def test_session_store_by_descriptor(agent_doc):
    agent = build(agent_doc, session_store={"kind": "memory", "options": {}})
    assert agent._coordinator._session_store is not None  # type: ignore[attr-defined]


@respx.mock
def test_session_store_by_instance(agent_doc):
    from computeragent.session_stores import InMemorySessionStore

    store = InMemorySessionStore()
    agent = build(agent_doc, session_store=store)

    assert agent._coordinator._session_store is store  # type: ignore[attr-defined]


@respx.mock
def test_no_session_store_by_default(agent_doc):
    agent = build(agent_doc)
    assert agent._coordinator._session_store is None  # type: ignore[attr-defined]


# ── identity loader ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_gap_loader_is_used_when_the_workspace_has_an_agent_yaml(agent_doc, tmp_path):
    (tmp_path / "agent.yaml").write_text("name: fixture\nversion: '1.0.0'\n", encoding="utf-8")
    engine = FakeEngine()

    async with build(agent_doc, source=tmp_path, engine=engine) as agent:
        await agent.chat("hi")

    assert engine.contexts[0].manifest.name == "fixture"


@pytest.mark.asyncio
@respx.mock
async def test_passthrough_loader_is_used_for_a_plain_workspace(agent_doc, tmp_path):
    """The GAP loader raises on a missing agent.yaml, so without the fallback a
    plain (non-GAP) repo would be unrunnable."""
    engine = FakeEngine()

    async with build(agent_doc, source=tmp_path, engine=engine) as agent:
        await agent.chat("hi")

    assert engine.contexts[0].manifest.name == tmp_path.name


# ── one turn end to end ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_one_chat_returns_a_result(agent_doc, tmp_path):
    async with build(agent_doc, source=tmp_path) as agent:
        result = await agent.chat("hello")

    assert result.session_id
    assert result.duration_ms >= 0


@pytest.mark.asyncio
@respx.mock
async def test_the_resolved_model_reaches_the_engine_context(agent_doc, tmp_path):
    engine = FakeEngine()

    async with build(agent_doc, source=tmp_path, engine=engine) as agent:
        await agent.chat("hello")

    assert engine.contexts[0].manifest.options["model"] == "6aa93f1a75ce74c4e6772b20"
