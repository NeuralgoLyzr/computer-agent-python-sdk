"""The Lyzr key is read at every turn: the engine's key variables and the
bound MCP servers' ``x-api-key`` follow it."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from computeragent.harness import ComputerAgent
from computeragent.harness.egress_token import CREDENTIAL
from computeragent.harness.settings import SettingsError
from computeragent.protocol_types import EngineCapabilities, EngineContext, EngineEvent

AGENT_ID = "6aa93f1b3817a13d3103d2c1"
BASE = "http://egress.internal/computer-agent-api/agentos/api"
URL = f"{BASE}/v1/agents/{AGENT_ID}"
LLM_URL = "http://egress.internal/llm"
MCP_URL = "http://egress.internal/mcp/v1"
GUARDRAILS = "http://egress.internal/guardrails"
KEY_NAMES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "COMPUTER_AGENT_EGRESS_PROXY_SECRET")


class RecordingEngine:
    # Not the Claude CLI's name, so the loopback forwarder stays out of it.
    name = "recording"
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
        async for _ in ctx.user_message_queue:
            break
        yield EngineEvent(kind="sdk_message", payload={"type": "assistant"})


def build(agent_doc, engine, **kwargs) -> ComputerAgent:
    agent_doc["mcp"] = {"servers": [{"server_id": "srv_1", "tool_name": "jira"}]}
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))
    respx.post(f"{GUARDRAILS}/v1/check").mock(
        return_value=httpx.Response(200, json={"data": {"decision": "allow"}})
    )
    params: dict = {
        "agent_id": AGENT_ID,
        "base_url": BASE,
        "engine": engine,
        "llm_url": LLM_URL,
        "mcp_url": MCP_URL,
        "guardrails_url": GUARDRAILS,
    }
    params.update(kwargs)
    return ComputerAgent(**params)


def keys_of(ctx: EngineContext) -> set[str]:
    return {ctx.envs[name] for name in KEY_NAMES}


@respx.mock
async def test_engine_env_follows_the_key(agent_doc, monkeypatch):
    monkeypatch.setenv("COMPUTER_AGENT_EGRESS_PROXY_SECRET", "sbt_1")
    engine = RecordingEngine()

    async with build(agent_doc, engine) as agent:
        await agent.chat("one")
        monkeypatch.setenv("COMPUTER_AGENT_EGRESS_PROXY_SECRET", "sbt_2")
        await agent.chat("two")

    first, second = engine.contexts
    assert keys_of(first) == {"sbt_1"}
    assert keys_of(second) == {"sbt_2"}
    for name in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):
        assert first.envs[name] == second.envs[name]


@respx.mock
async def test_mcp_headers_follow_the_key(agent_doc, monkeypatch):
    monkeypatch.setenv("COMPUTER_AGENT_EGRESS_PROXY_SECRET", "sbt_1")
    engine = RecordingEngine()
    mine = {"type": "http", "url": "http://elsewhere/mcp", "headers": {"x-api-key": "own"}}

    async with build(agent_doc, engine) as agent:
        agent._coordinator._options_overlay["mcp_servers"] = {  # type: ignore[attr-defined]
            "jira": agent._coordinator._mcp_servers["jira"],  # type: ignore[attr-defined]
            "mine": mine,
        }
        await agent.chat("one")
        monkeypatch.setenv("COMPUTER_AGENT_EGRESS_PROXY_SECRET", "sbt_2")
        await agent.chat("two")

    servers = engine.contexts[1].manifest.options["mcp_servers"]
    assert servers["jira"]["headers"]["x-api-key"] == "sbt_2"
    assert servers["mine"]["headers"]["x-api-key"] == "own"
    assert (
        engine.contexts[0].manifest.options["mcp_servers"]["jira"]["headers"]["x-api-key"]
        == "sbt_1"
    )


@respx.mock
async def test_explicit_api_key_follows_its_reissue(agent_doc, monkeypatch):
    monkeypatch.setenv("COMPUTER_AGENT_EGRESS_PROXY_SECRET", "sbt_env")
    engine = RecordingEngine()

    async with build(agent_doc, engine, api_key="sbt_k") as agent:
        await agent.chat("one")
        monkeypatch.setenv("COMPUTER_AGENT_EGRESS_PROXY_SECRET", "sbt_env_2")
        await agent.chat("two")
        expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        assert CREDENTIAL.adopt("sbt_k", "sbt_k2", expiry)
        await agent.chat("three")

    assert [keys_of(c) for c in engine.contexts] == [{"sbt_k"}, {"sbt_k"}, {"sbt_k2"}]


@respx.mock
async def test_missing_key_fails_the_turn(agent_doc, monkeypatch):
    monkeypatch.setenv("COMPUTER_AGENT_EGRESS_PROXY_SECRET", "sbt_1")
    engine = RecordingEngine()

    async with build(agent_doc, engine) as agent:
        await agent.chat("one")
        monkeypatch.delenv("COMPUTER_AGENT_EGRESS_PROXY_SECRET")
        with pytest.raises(SettingsError, match="COMPUTER_AGENT_EGRESS_PROXY_SECRET") as exc:
            await agent.chat("two")

    assert len(engine.contexts) == 1
    assert "sbt_1" not in str(exc.value)
