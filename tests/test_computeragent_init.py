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
from computeragent.harness.settings import SettingsError
from computeragent.protocol_types import EngineCapabilities, EngineContext, EngineEvent

AGENT_ID = "6aa93f1b3817a13d3103d2c1"
BASE = "http://localhost:8788/agentos/api"
URL = f"{BASE}/v1/agents/{AGENT_ID}"
KEY = "ags_testkey"
LLM_URL = "http://egress.internal/llm"
MCP_URL = "http://egress.internal/mcp"
GUARDRAILS = "http://egress.internal/guardrails"
CHECK_URL = f"{GUARDRAILS}/v1/check"


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
    respx.post(CHECK_URL).mock(
        return_value=httpx.Response(200, json={"data": {"decision": "allow"}})
    )
    params: dict = {
        "agent_id": AGENT_ID,
        "base_url": BASE,
        "api_key": KEY,
        "llm_url": LLM_URL,
        "mcp_url": MCP_URL,
        "guardrails_url": "http://egress.internal/guardrails",
        "engine": FakeEngine(),
    }
    params.update(kwargs)
    return ComputerAgent(**params)


# ── resolution ───────────────────────────────────────────────────────────────


@respx.mock
def test_exposes_the_resolved_agent(agent_doc):
    agent = build(agent_doc)

    assert agent.agent.agent_id == AGENT_ID
    assert agent.agent.model == "claude-sonnet-4-5"
    assert not hasattr(agent.agent, "has_native_gateway")


@respx.mock
def test_resolution_failure_surfaces_at_construction(agent_doc):
    """Better a failed constructor than a half-built agent that dies on chat."""
    respx.get(URL).mock(return_value=httpx.Response(404, json={"error": {"code": "NOT_FOUND"}}))

    with pytest.raises(AgentResolveError):
        ComputerAgent(
            agent_id=AGENT_ID,
            base_url=BASE,
            api_key=KEY,
            engine=FakeEngine(),
        )


def test_missing_llm_url_is_refused_at_construction():
    with pytest.raises(SettingsError, match="llm_url"):
        ComputerAgent(agent_id=AGENT_ID, base_url=BASE, api_key=KEY, engine=FakeEngine())


# ── engine selection ─────────────────────────────────────────────────────────


def test_engine_is_required():
    """Not inferred from the agent document: `engine` also selects which
    gateway-env branch is built, so a document change must not be able to
    re-shape ANTHROPIC_BASE_URL under a caller who never asked."""
    with pytest.raises(SettingsError, match="engine"):
        ComputerAgent(
            agent_id=AGENT_ID,
            base_url=BASE,
            api_key=KEY,
            llm_url=LLM_URL,
            mcp_url=MCP_URL,
            guardrails_url=GUARDRAILS,
        )


@respx.mock
def test_a_named_engine_resolves_to_the_registered_driver(agent_doc):
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    agent = ComputerAgent(
        agent_id=AGENT_ID,
        base_url=BASE,
        api_key=KEY,
        llm_url=LLM_URL,
        mcp_url=MCP_URL,
        guardrails_url=GUARDRAILS,
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
            llm_url=LLM_URL,
            mcp_url=MCP_URL,
            guardrails_url=GUARDRAILS,
            engine="langgraph",
        )


@respx.mock
def test_an_engine_that_differs_from_the_harness_is_logged(agent_doc, caplog):
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    with caplog.at_level("WARNING", logger="computeragent.harness"):
        ComputerAgent(
            agent_id=AGENT_ID,
            base_url=BASE,
            api_key=KEY,
            engine="gitagent",
            llm_url=LLM_URL,
            mcp_url=MCP_URL,
            guardrails_url=GUARDRAILS,
        )

    assert any("overrides agent" in r.getMessage() for r in caplog.records)


@respx.mock
def test_engine_can_be_named(agent_doc):
    """The engine follows the caller, not the agent's declared harness."""
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    agent = ComputerAgent(
        agent_id=AGENT_ID,
        base_url=BASE,
        api_key=KEY,
        engine="gitagent",
        llm_url=LLM_URL,
        mcp_url=MCP_URL,
        guardrails_url=GUARDRAILS,
    )

    assert agent._coordinator._engine.name == "gitagent"  # type: ignore[attr-defined]


@respx.mock
def test_engine_can_be_an_instance(agent_doc):
    engine = FakeEngine()
    agent = build(agent_doc, engine=engine)

    assert agent._coordinator._engine is engine  # type: ignore[attr-defined]


# ── workspace ────────────────────────────────────────────────────────────────


@respx.mock
def test_the_workspace_is_the_working_directory(agent_doc):
    """The agent is baked into the image: nothing is cloned, and the agent
    document's own source is never read."""
    agent = build(agent_doc)

    assert agent._coordinator._source == Path.cwd()  # type: ignore[attr-defined]


@respx.mock
def test_source_and_git_token_are_gone(agent_doc):
    with pytest.raises(TypeError, match="source"):
        build(agent_doc, source=Path.cwd())
    with pytest.raises(TypeError, match="git_token"):
        build(agent_doc, git_token="ghp_x")


@respx.mock
def test_gap_dir_is_the_workspace_in_gap_mode(agent_doc, tmp_path):
    (tmp_path / "agent.yaml").write_text("name: fixture\n", encoding="utf-8")

    agent = build(agent_doc, gap_dir=tmp_path)

    assert agent._coordinator._source == tmp_path  # type: ignore[attr-defined]


@respx.mock
def test_gap_dir_as_a_string_is_accepted(agent_doc, tmp_path):
    (tmp_path / "agent.yaml").write_text("name: fixture\n", encoding="utf-8")

    agent = build(agent_doc, gap_dir=str(tmp_path))

    assert agent._coordinator._source == tmp_path  # type: ignore[attr-defined]


# ── what the coordinator is handed ───────────────────────────────────────────


@respx.mock
def test_gateway_envs_reach_the_coordinator(agent_doc):
    agent = build(agent_doc)
    envs = agent._coordinator._envs  # type: ignore[attr-defined]

    assert envs["ANTHROPIC_API_KEY"] == KEY
    assert envs["LYZR_API_KEY"] == KEY
    assert envs["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] == "1"
    assert envs["ANTHROPIC_BASE_URL"] == LLM_URL


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
    assert overlay["model"] == "claude-sonnet-4-5"
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
async def test_gap_mode_loads_the_agent_yaml(agent_doc, tmp_path):
    (tmp_path / "agent.yaml").write_text("name: fixture\nversion: '1.0.0'\n", encoding="utf-8")
    engine = FakeEngine()

    async with build(agent_doc, gap_dir=tmp_path, engine=engine) as agent:
        await agent.chat("hi")

    assert engine.contexts[0].manifest.name == "fixture"


@pytest.mark.asyncio
@respx.mock
async def test_without_gap_dir_an_agent_yaml_in_the_cwd_is_ignored(
    agent_doc, tmp_path, monkeypatch
):
    """GAP mode is explicit: an agent.yaml that happens to sit in the working
    directory does not switch it on."""
    (tmp_path / "agent.yaml").write_text("name: fixture\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    engine = FakeEngine()

    async with build(agent_doc, engine=engine) as agent:
        await agent.chat("hi")

    assert engine.contexts[0].manifest.name == tmp_path.name


# ── one turn end to end ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_one_chat_returns_a_result(agent_doc, tmp_path):
    async with build(agent_doc) as agent:
        result = await agent.chat("hello")

    assert result.session_id
    assert result.duration_ms >= 0


@pytest.mark.asyncio
@respx.mock
async def test_the_resolved_model_reaches_the_engine_context(agent_doc, tmp_path):
    engine = FakeEngine()

    async with build(agent_doc, engine=engine) as agent:
        await agent.chat("hello")

    assert engine.contexts[0].manifest.options["model"] == "claude-sonnet-4-5"
