"""MCP servers bound in AgentOS, served through ``mcp_url``.

Mirrors CAS ``mcpDescriptorsFor`` + ``applyMcpServers``: engine mode only, the
binding's ``tool_name`` as the namespace, the caller's own server for a sign-in
binding (or the tool withheld), and ``allowed_tools`` grants for
Anthropic-shaped engines only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from computeragent.harness import ComputerAgent, UnsupportedAgentError
from computeragent.harness.mcp import (
    McpBinding,
    McpBindingError,
    build_mcp_servers,
    parse_bindings,
)
from computeragent.harness.settings import SettingsError
from computeragent.protocol_types import EngineCapabilities, EngineContext, EngineEvent

AGENT_ID = "6aa93f1b3817a13d3103d2c1"
BASE = "http://localhost:8788/agentos/api"
URL = f"{BASE}/v1/agents/{AGENT_ID}"
KEY = "ags_testkey"
LLM_URL = "http://egress.internal/llm"
MCP_URL = "http://egress.internal/mcp"


def build_servers(bindings, *, mcp_mode="engine", anthropic_shaped=True):
    return build_mcp_servers(
        tuple(bindings),
        mcp_mode=mcp_mode,
        mcp_url=MCP_URL,
        api_key=KEY,
        anthropic_shaped=anthropic_shaped,
    )


# ── parsing the agent document ───────────────────────────────────────────────


def test_parses_every_field():
    (binding,) = parse_bindings(
        [
            {
                "server_id": "srv_creator",
                "tool_name": "jira",
                "action_names": ["search", "create"],
                "persist_auth": False,
                "your_server_id": "srv_mine",
                "needs_your_connection": False,
            }
        ]
    )

    assert binding == McpBinding(
        server_id="srv_creator",
        tool_name="jira",
        action_names=("search", "create"),
        persist_auth=False,
        your_server_id="srv_mine",
    )


def test_no_bindings():
    assert parse_bindings(None) == ()
    assert parse_bindings([]) == ()


def test_persist_auth_defaults_to_shared():
    (binding,) = parse_bindings([{"server_id": "s", "tool_name": "t"}])
    assert binding.persist_auth is True


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("nope", "must be a list"),
        (["nope"], r"mcp_servers\[0\] must be an object"),
        ([{"tool_name": "t"}], r"mcp_servers\[0\] has no server_id"),
        ([{"server_id": "s"}], r"mcp_servers\[0\] \(s\) has no tool_name"),
        ([{"server_id": "  ", "tool_name": "t"}], "no server_id"),
    ],
)
def test_a_malformed_binding_is_refused(raw, match):
    with pytest.raises(McpBindingError, match=match):
        parse_bindings(raw)


# ── building engine servers ──────────────────────────────────────────────────


def test_a_shared_binding_becomes_a_server_on_mcp_url():
    servers, allowed = build_servers([McpBinding("srv_1", "jira", ("search", "create"))])

    assert servers == {
        "jira": {
            "type": "http",
            "url": f"{MCP_URL}/srv_1?tools=search%2Ccreate",
            "headers": {"x-api-key": KEY},
            "alwaysLoad": True,
        }
    }
    assert allowed == ["mcp__jira"]


def test_no_action_names_means_no_tools_query():
    servers, _ = build_servers([McpBinding("srv_1", "jira")])
    assert servers["jira"]["url"] == f"{MCP_URL}/srv_1"


def test_server_id_and_tools_are_url_encoded():
    servers, _ = build_servers([McpBinding("a/b c", "x", ("t&1",))])
    assert servers["x"]["url"] == f"{MCP_URL}/a%2Fb%20c?tools=t%261"


def test_a_sign_in_binding_uses_the_callers_own_server():
    servers, _ = build_servers(
        [McpBinding("srv_creator", "jira", persist_auth=False, your_server_id="srv_mine")]
    )

    assert servers["jira"]["url"] == f"{MCP_URL}/srv_mine"


def test_a_sign_in_binding_without_a_connection_is_withheld(caplog):
    with caplog.at_level("WARNING", logger="computeragent.harness.mcp"):
        servers, allowed = build_servers(
            [
                McpBinding("srv_creator", "jira", persist_auth=False),
                McpBinding("srv_2", "github"),
            ]
        )

    assert list(servers) == ["github"]
    assert allowed == ["mcp__github"]
    assert any("jira" in r.getMessage() for r in caplog.records)


def test_studio_mode_attaches_nothing():
    assert build_servers([McpBinding("srv_1", "jira")], mcp_mode="studio") == ({}, [])


def test_gitagent_gets_servers_but_no_allowed_tools_entries():
    """gitclaw treats allowedTools as a strict allowlist; an mcp__ entry would
    filter out every tool it has."""
    servers, allowed = build_servers([McpBinding("srv_1", "jira")], anthropic_shaped=False)

    assert "jira" in servers
    assert allowed == []


def test_a_trailing_slash_on_mcp_url_does_not_double_up():
    servers, _ = build_mcp_servers(
        (McpBinding("srv_1", "jira"),),
        mcp_mode="engine",
        mcp_url=MCP_URL + "/",
        api_key=KEY,
        anthropic_shaped=True,
    )
    assert servers["jira"]["url"] == f"{MCP_URL}/srv_1"


# ── through ComputerAgent ────────────────────────────────────────────────────


class FakeEngine:
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
        async for _ in ctx.user_message_queue:
            break
        yield EngineEvent(
            kind="sdk_message", payload={"type": "assistant", "message": {"content": "ok"}}
        )


def build(agent_doc, **kwargs) -> ComputerAgent:
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))
    params: dict = {
        "agent_id": AGENT_ID,
        "base_url": BASE,
        "api_key": KEY,
        "engine": FakeEngine(),
        "llm_url": LLM_URL,
        "mcp_url": MCP_URL,
    }
    params.update(kwargs)
    return ComputerAgent(**params)


def test_mcp_url_is_required():
    with pytest.raises(SettingsError, match="mcp_url"):
        ComputerAgent(
            agent_id=AGENT_ID, base_url=BASE, api_key=KEY, engine="gitagent", llm_url=LLM_URL
        )


@pytest.mark.asyncio
@respx.mock
async def test_bound_servers_reach_the_engine(agent_doc):
    agent_doc["mcp_servers"] = [{"server_id": "srv_1", "tool_name": "jira"}]
    engine = FakeEngine()

    async with build(agent_doc, engine=engine) as agent:
        await agent.chat("hi")

    options = engine.contexts[0].manifest.options
    assert options["mcp_servers"]["jira"]["url"] == f"{MCP_URL}/srv_1"
    assert "mcp__jira" in options["allowed_tools"]


@pytest.mark.asyncio
@respx.mock
async def test_gap_tools_and_bound_servers_are_both_kept(agent_doc, tmp_path):
    """The bound servers are added to the loader's own, not swapped for them."""
    (tmp_path / "agent.yaml").write_text("name: fixture\ntools: [Read]\n", encoding="utf-8")
    agent_doc["mcp_servers"] = [{"server_id": "srv_1", "tool_name": "jira"}]
    engine = FakeEngine()

    async with build(agent_doc, engine=engine, gap_dir=tmp_path) as agent:
        await agent.chat("hi")

    options = engine.contexts[0].manifest.options
    assert "jira" in options["mcp_servers"]
    assert options["allowed_tools"] == ["Read", "mcp__jira"]


@pytest.mark.asyncio
@respx.mock
async def test_a_caller_mcp_servers_option_still_wins(agent_doc):
    agent_doc["mcp_servers"] = [{"server_id": "srv_1", "tool_name": "jira"}]
    engine = FakeEngine()

    async with build(agent_doc, engine=engine, options={"mcp_servers": {}}) as agent:
        await agent.chat("hi")

    assert engine.contexts[0].manifest.options["mcp_servers"] == {}


@pytest.mark.asyncio
@respx.mock
async def test_no_bindings_leaves_mcp_servers_unset(agent_doc):
    engine = FakeEngine()

    async with build(agent_doc, engine=engine) as agent:
        await agent.chat("hi")

    assert "mcp_servers" not in engine.contexts[0].manifest.options


@respx.mock
def test_agent_yaml_mcp_servers_are_refused(agent_doc, tmp_path):
    """They name their own URL, around the egress proxy."""
    (tmp_path / "agent.yaml").write_text(
        "name: fixture\nmcp_servers:\n  docs:\n    url: https://mcp.example.com\n",
        encoding="utf-8",
    )

    with pytest.raises(UnsupportedAgentError, match="docs"):
        build(agent_doc, gap_dir=tmp_path)
