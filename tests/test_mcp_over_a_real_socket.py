"""The client and the builder against a real HTTP server.

Everything else mocks the transport. This file does not, because two things are
only true over a socket: that the request we believe we send is the request that
arrives, and that the fake facade the e2e script depends on actually behaves
like the client expects. A double that drifts from the thing it doubles is worse
than no double, so it is pinned here rather than assumed.
"""

from __future__ import annotations

import json

import pytest

from computeragent.mcp.binding import McpBinding
from computeragent.mcp.client import LyzrMcpClient, McpServiceError
from computeragent.mcp.server import build_mcp_servers
from fakes.lyzr_mcp_facade import FakeLyzrMcpFacade

KEY = "ags_socket_test"
BINDING = McpBinding(
    server_id="srv_1",
    action_names=("lookup_sentinel",),
    tool_name="Project",
    persist_auth=False,
)


@pytest.fixture
def facade():
    with FakeLyzrMcpFacade() as f:
        yield f


def client_for(facade, **kw):
    kw.setdefault("base_url", facade.base_url)
    kw.setdefault("api_key", KEY)
    return LyzrMcpClient(**kw)


async def test_discovery_reaches_the_expected_route(facade):
    tools = await client_for(facade).list_tools("srv_1")

    method, path, headers, _ = facade.requests[0]
    assert (method, path) == ("GET", "/api/v1/mcp/servers/srv_1/tools")
    assert headers["x-api-key"] == KEY
    assert {t.name for t in tools} == {"lookup_sentinel", "unbound_tool"}


async def test_execution_reaches_the_expected_route(facade):
    await client_for(facade).execute("srv_1", "lookup_sentinel", {"reason": "e2e"})

    method, path, _, body = facade.requests[-1]
    assert (method, path) == ("POST", "/api/v1/mcp/tools/execute")
    assert body == {
        "server_id": "srv_1",
        "tool_name": "lookup_sentinel",
        "arguments": {"reason": "e2e"},
    }


async def test_a_missing_credential_is_rejected_by_the_facade(facade):
    """Mirrors the hosted bridge, which 401s a request presenting none and has
    no operator token to fall back on."""
    with pytest.raises(McpServiceError, match="401"):
        await client_for(facade, api_key="").list_tools("srv_1")


async def test_the_whole_path_produces_a_callable_tool_carrying_the_sentinel(facade):
    """The end-to-end property, minus the model: binding → discovery → an
    in-process MCP tool → a real HTTP call → content the model can read.

    The sentinel is the point. Without one, "the tool returned data" and "the
    model produced something plausible" are indistinguishable.
    """
    built = await build_mcp_servers((BINDING,), client_for(facade))

    assert list(built.servers) == ["Project"]
    assert built.tool_names == ["mcp__Project__lookup_sentinel"]

    (tool,) = built.tools["Project"]
    out = await tool.handler({"reason": "e2e"})

    assert out["is_error"] is False
    blob = json.dumps(out["content"])
    assert facade.sentinel in blob


async def test_the_structured_block_survives_as_text(facade):
    """The facade's response deliberately mixes a text block with a `structured`
    one — the shape that broke the TS bridge. Hoisting it the way the TS does
    would delete it here, because `create_sdk_mcp_server` never forwards
    `structuredContent`."""
    built = await build_mcp_servers((BINDING,), client_for(facade))
    (tool,) = built.tools["Project"]

    out = await tool.handler({})

    assert len(out["content"]) == 2
    assert all(block["type"] == "text" for block in out["content"])
    # Both the human-readable and the machine-readable payload reach the model.
    assert out["content"].count({"type": "text", "text": f"sentinel: {facade.sentinel}"}) == 1
    assert json.loads(out["content"][1]["text"])["data"] == {"sentinel": facade.sentinel}


async def test_an_unbound_tool_is_not_exposed(facade):
    """The facade offers two tools; the binding names one. The other must not
    appear — the allowlist is the agent's actual grant."""
    built = await build_mcp_servers((BINDING,), client_for(facade))

    assert [t.name for t in built.tools["Project"]] == ["lookup_sentinel"]


async def test_a_tool_that_fails_upstream_becomes_a_readable_error(facade):
    """`success: false` is a tool that RAN and failed. The model should see it
    and adapt rather than receive a fault it cannot recover from."""
    binding = McpBinding(
        server_id="srv_1", action_names=("unbound_tool",), tool_name="P2", persist_auth=False
    )
    built = await build_mcp_servers((binding,), client_for(facade))
    (tool,) = built.tools["P2"]

    out = await tool.handler({})

    assert out["is_error"] is True
    assert "no such tool" in out["content"][0]["text"]


async def test_a_dead_facade_degrades_the_run_instead_of_failing_it():
    """Started and stopped, so the port is closed but plausible — the closest
    thing to the real outage mode."""
    with FakeLyzrMcpFacade() as f:
        base = f.base_url

    built = await build_mcp_servers((BINDING,), LyzrMcpClient(base_url=base, api_key=KEY))

    assert built.servers == {}
