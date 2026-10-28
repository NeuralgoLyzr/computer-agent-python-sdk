"""The MCP checks that depend on the caller's arguments, not the document.

`ComputerAgent.__init__` is where "the agent document is fine, your arguments
are not" lives — it is already where the `llm_proxy_url` check sits, for the
same reason. Refusing here beats booting: an agent that silently runs without
its tools is indistinguishable from one whose model never chose a tool, which
is the failure class the binding layer exists to eliminate.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from computeragent.harness import ComputerAgent
from computeragent.harness.agentos_agent import AgentResolveError

AGENT_ID = "6aa93f1b3817a13d3103d2c1"
BASE = "http://localhost:8788/agentos/api"
URL = f"{BASE}/v1/agents/{AGENT_ID}"
KEY = "ags_test"
PROXY = "http://127.0.0.1:8786"
MCP_BASE = "https://mcp.example.invalid"

BOUND = [{"server_id": "srv_1", "action_names": ["get_weather"], "tool_name": "Weather"}]


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


@pytest.fixture(autouse=True)
def _no_ambient_mcp_base(monkeypatch):
    """The env fallback would otherwise make these tests pass or fail depending
    on the developer's shell."""
    monkeypatch.delenv("LYZR_MCP_CLIENT_URL", raising=False)


@respx.mock
def test_bindings_without_an_mcp_base_are_refused(agent_doc):
    agent_doc["mcp_servers"] = BOUND

    with pytest.raises(AgentResolveError, match="no MCP service base is configured"):
        build(agent_doc)


@respx.mock
def test_the_refusal_names_the_servers_and_the_two_ways_to_fix_it(agent_doc):
    agent_doc["mcp_servers"] = BOUND

    with pytest.raises(AgentResolveError) as excinfo:
        build(agent_doc)

    message = str(excinfo.value)
    assert "Weather" in message
    assert "mcp_base_url" in message
    assert "LYZR_MCP_CLIENT_URL" in message


@respx.mock
def test_an_explicit_base_satisfies_it(agent_doc):
    agent_doc["mcp_servers"] = BOUND

    agent = build(agent_doc, mcp_base_url=MCP_BASE)

    assert agent._mcp_base_url == MCP_BASE


@respx.mock
def test_the_environment_satisfies_it_too(agent_doc, monkeypatch):
    """Same variable the hosted stack sets, so an operator who has already
    configured the CAS gets this for free."""
    monkeypatch.setenv("LYZR_MCP_CLIENT_URL", MCP_BASE)
    agent_doc["mcp_servers"] = BOUND

    assert build(agent_doc)._mcp_base_url == MCP_BASE


@respx.mock
def test_an_explicit_base_beats_the_environment(agent_doc, monkeypatch):
    monkeypatch.setenv("LYZR_MCP_CLIENT_URL", "https://wrong.example.invalid")
    agent_doc["mcp_servers"] = BOUND

    assert build(agent_doc, mcp_base_url=MCP_BASE)._mcp_base_url == MCP_BASE


@respx.mock
def test_an_empty_env_value_does_not_count_as_configured(agent_doc, monkeypatch):
    """An exported-but-blank variable is the classic way to believe something is
    set when it is not."""
    monkeypatch.setenv("LYZR_MCP_CLIENT_URL", "")
    agent_doc["mcp_servers"] = BOUND

    with pytest.raises(AgentResolveError, match="no MCP service base"):
        build(agent_doc)


@respx.mock
def test_an_agent_with_no_bindings_needs_no_base(agent_doc):
    """The overwhelmingly common case must not acquire a new required argument."""
    agent = build(agent_doc)

    assert agent.agent.mcp_bindings == ()
    assert agent._mcp_base_url is None
