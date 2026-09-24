"""Choosing between the two MCP paths."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from computeragent.harness import ComputerAgent

AGENT_ID = "6aa93f1b3817a13d3103d2c1"
BASE = "http://localhost:8788/agentos/api"
URL = f"{BASE}/v1/agents/{AGENT_ID}"
KEY = "ags_test"
BOUND = [{"server_id": "srv_1", "action_names": ["get_weather"], "tool_name": "Weather"}]


@pytest.fixture(autouse=True)
def _no_ambient_mcp_base(monkeypatch):
    monkeypatch.delenv("LYZR_MCP_CLIENT_URL", raising=False)


def build(agent_doc, **kw):
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))
    params = {
        "agent_id": AGENT_ID,
        "base_url": BASE,
        "api_key": KEY,
        "engine": "claude-agent-sdk",
        "llm_proxy_url": "http://127.0.0.1:8786",
        "source": Path.cwd(),
    }
    params.update(kw)
    return ComputerAgent(**params)


@respx.mock
def test_a_bridge_url_alone_satisfies_the_configuration_check(agent_doc):
    agent_doc["mcp_servers"] = BOUND

    agent = build(agent_doc, mcp_bridge_url="https://proxy.invalid")

    assert agent._coordinator._mcp_bridge_servers is not None
    assert agent._coordinator._mcp_client is None


@respx.mock
def test_the_bridge_wins_over_a_configured_facade_base(agent_doc):
    """It is an explicit override, so it has to outrank the ambient default —
    otherwise setting it would appear to do nothing."""
    agent_doc["mcp_servers"] = BOUND

    agent = build(
        agent_doc, mcp_base_url="https://mcp.invalid", mcp_bridge_url="https://proxy.invalid"
    )

    assert agent._coordinator._mcp_client is None
    servers = agent._coordinator._mcp_bridge_servers.servers
    assert servers["Weather"]["url"].startswith("https://proxy.invalid/mcp/")


@respx.mock
def test_the_facade_is_used_when_no_bridge_is_named(agent_doc):
    agent_doc["mcp_servers"] = BOUND

    agent = build(agent_doc, mcp_base_url="https://mcp.invalid")

    assert agent._coordinator._mcp_bridge_servers is None
    assert agent._coordinator._mcp_client is not None


@respx.mock
def test_neither_is_built_for_an_agent_with_no_bindings(agent_doc):
    agent = build(agent_doc, mcp_bridge_url="https://proxy.invalid")

    assert agent._coordinator._mcp_bridge_servers is None
    assert agent._coordinator._mcp_client is None
