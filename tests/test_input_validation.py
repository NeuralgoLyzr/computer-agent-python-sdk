"""Inputs that used to fail silently, late, or with the wrong error.

Each case here was found by a live usage survey. The common thread is that the
SDK accepted something unusable and only failed much later — or, worse, did
something plausible and wrong. Construction is where these belong: it is cheap,
and the error can still name the argument.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from computeragent.harness import ComputerAgent
from computeragent.harness.agentos_agent import AgentResolveError, api_base
from computeragent.harness.errors import UnknownEngineError, UnknownSessionStoreError
from computeragent.harness.gateway_envs import GatewayConfigError

AGENT_ID = "6aa93f1b3817a13d3103d2c1"
BASE = "http://localhost:8788/agentos/api"
URL = f"{BASE}/v1/agents/{AGENT_ID}"
KEY = "ags_test"
PROXY = "http://127.0.0.1:8786"


def build(agent_doc, **kw):
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))
    params = {
        "agent_id": AGENT_ID,
        "base_url": BASE,
        "api_key": KEY,
        "llm_proxy_url": PROXY,
        "source": Path.cwd(),
    }
    params.update(kw)
    return ComputerAgent(**params)


# ── base_url ─────────────────────────────────────────────────────────────────


def test_non_string_base_url_gets_a_typed_error():
    """Every sibling bad input got a curated error; this one raised a bare
    AttributeError from inside the normaliser."""
    with pytest.raises(AgentResolveError, match="must be a string"):
        api_base(None)  # type: ignore[arg-type]


def test_an_unrecognised_path_is_refused_rather_than_appended_to():
    """A one-character typo used to produce
    `…/agentos/apiv1/agentos/api/v1`, and the resulting 404 blamed the
    caller's agent id — sending them to look in entirely the wrong place."""
    with pytest.raises(AgentResolveError, match="path this SDK does not recognise"):
        api_base("http://h:8788/agentos/apiv1")


@pytest.mark.parametrize(
    "given",
    [
        "http://h:8788",
        "http://h:8788/",
        "http://h:8788/agentos/api",
        "http://h:8788/agentos/api/v1",
    ],
)
def test_the_supported_spellings_still_normalise(given):
    assert api_base(given) == "http://h:8788/agentos/api/v1"


# ── source ───────────────────────────────────────────────────────────────────


@respx.mock
def test_empty_source_is_refused(agent_doc):
    """`Path("") == Path(".")`, so a blank config value silently pointed a
    bypassPermissions agent at whatever directory the process was in."""
    with pytest.raises(AgentResolveError, match="empty string"):
        build(agent_doc, source="")


@respx.mock
def test_nonexistent_source_path_is_refused_at_construction(agent_doc):
    with pytest.raises(AgentResolveError, match="does not exist"):
        build(agent_doc, source=Path("/no/such/directory/anywhere"))


@respx.mock
def test_source_that_is_a_file_is_refused(agent_doc, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")

    with pytest.raises(AgentResolveError, match="not a directory"):
        build(agent_doc, source=f)


@respx.mock
def test_source_of_the_wrong_type_is_refused_at_construction(agent_doc):
    with pytest.raises(AgentResolveError, match="source must be"):
        build(agent_doc, source=42)


@respx.mock
def test_a_git_url_source_is_accepted_without_touching_the_network(agent_doc):
    """Validation must not try to reach a remote — the clone happens later."""
    build(agent_doc, source="https://github.com/org/repo.git")


# ── llm_proxy_url ────────────────────────────────────────────────────────────


@respx.mock
def test_a_malformed_proxy_url_is_refused(agent_doc):
    """It is concatenated into ANTHROPIC_BASE_URL, so garbage used to be
    accepted silently and fail only when the engine tried to reach it."""
    with pytest.raises(GatewayConfigError, match="http\\(s\\) URL"):
        build(agent_doc, llm_proxy_url="not-a-url")


# ── things that are not names ────────────────────────────────────────────────


@respx.mock
def test_a_non_driver_engine_says_so(agent_doc):
    """`engine=object()` used to be repr'd into "unknown engine <object …>",
    reading as though the user had typed a strange string."""
    with pytest.raises(UnknownEngineError, match="EngineDriver instance"):
        build(agent_doc, engine=object())


@respx.mock
def test_a_non_store_session_store_says_so(agent_doc):
    with pytest.raises(UnknownSessionStoreError, match="store instance"):
        build(agent_doc, session_store=object())


# ── inert callback ───────────────────────────────────────────────────────────


@respx.mock
def test_on_tool_call_warns_that_it_is_inert_on_the_claude_engine(agent_doc, caplog):
    """A caller passing only this believes they have a tool gate and has none."""

    async def cb(_req):
        return {"behavior": "allow"}

    with caplog.at_level("WARNING"):
        build(agent_doc, on_tool_call=cb)

    assert any("will not be consulted" in r.getMessage() for r in caplog.records)


# ── cancel before the run starts ─────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_cancelling_before_iteration_prevents_the_run(agent_doc, tmp_path):
    """It used to be a silent no-op, after which the turn ran to completion —
    the opposite of what was asked."""
    from test_computeragent_init import FakeEngine

    engine = FakeEngine()
    async with build(agent_doc, source=tmp_path, engine=engine) as agent:
        handle = agent.chat("do not run")
        await handle.cancel()
        events = [e async for e in handle]

    assert events == []
    assert engine.contexts == []


# ── parameters that now mean what they say ───────────────────────────────────


@respx.mock
def test_budget_max_usd_reaches_the_option_the_cli_enforces(agent_doc):
    """It used to be carried to the engine context and read by nothing."""
    agent = build(agent_doc, budget_max_usd=0.25)

    assert agent._coordinator._options_overlay["max_budget_usd"] == 0.25


@respx.mock
def test_an_explicit_option_beats_the_budget_shortcut(agent_doc):
    agent = build(agent_doc, budget_max_usd=0.25, options={"max_budget_usd": 1.0})

    assert agent._coordinator._options_overlay["max_budget_usd"] == 1.0


@respx.mock
def test_project_key_is_gone(agent_doc):
    """It never reached the engine — the store key is the SDK's, derived from
    cwd — so the parameter was a knob wired to nothing."""
    with pytest.raises(TypeError, match="project_key"):
        build(agent_doc, project_key="anything")


@respx.mock
def test_an_inline_source_with_files_is_refused(agent_doc):
    """The files are not materialised and the workspace falls back to cwd, so
    the agent would run against the caller's directory instead."""
    with pytest.raises(AgentResolveError, match="files"):
        build(agent_doc, source={"name": "x", "version": "1", "files": {"a.txt": "hi"}})


@respx.mock
def test_an_inline_source_without_files_is_still_accepted(agent_doc):
    build(agent_doc, source={"name": "x", "version": "1"})


# ── one conversation, one turn at a time ─────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_concurrent_turns_on_one_instance_are_refused(agent_doc, tmp_path):
    """They used to interleave silently. Since turns resume the previous engine
    session, two at once would share one session, workspace and store key."""
    import asyncio

    from computeragent.harness.errors import HarnessError
    from test_computeragent_init import FakeEngine

    class SlowEngine(FakeEngine):
        async def start_session(self, ctx):
            self.contexts.append(ctx)
            await asyncio.sleep(0.2)
            async for _ in ctx.user_message_queue:
                break
            yield __import__("computeragent.protocol_types", fromlist=["EngineEvent"]).EngineEvent(
                kind="sdk_message", payload={"type": "assistant"}
            )

    async with build(agent_doc, source=tmp_path, engine=SlowEngine()) as agent:
        first = asyncio.create_task(agent.chat("one").result())
        await asyncio.sleep(0.05)

        with pytest.raises(HarnessError, match="already in flight"):
            await agent.chat("two")

        await first


@pytest.mark.asyncio
@respx.mock
async def test_sequential_turns_are_unaffected(agent_doc, tmp_path):
    """The guard must not break the normal multi-turn case."""
    from test_computeragent_init import FakeEngine

    engine = FakeEngine()
    async with build(agent_doc, source=tmp_path, engine=engine) as agent:
        await agent.chat("one")
        await agent.chat("two")

    assert len(engine.contexts) == 2
