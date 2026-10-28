"""The real Lyzr MCP facade. Skipped unless it is configured.

This is the only place the real service is touched. It exists to answer the one
question no double can: **does a workspace `api_key` authenticate against
`/api/v1/mcp/*`?**

The doubt was specific. The hosted bridge forwards the caller's key verbatim to
the MCP service (`mcp.ts:352-355`), while AgentOS's own bind-time validation
reaches a *different* host over a *different* path — the Studio proxy's
`/v3/tools/mcp/*` — with a Studio key (`lyzr-studio-client.ts:223-226`). The two
are documented as returning identical bodies for the reads, but only the first
has an execute route, and no single credential had been shown to work against
both.

**Probed 2026-09-16 against `https://mcp-dev.test.studio.lyzr.ai`, and the
question is STILL OPEN.** The list route is not a usable auth oracle:

    no x-api-key header   → 422 {"detail":[{"type":"missing", …}]}   (FastAPI)
    x-api-key: garbage    → 200 {"servers": [], "total": 0}
    x-api-key: real key   → 200 {"servers": [], "total": 0}

A bogus credential and a real one are **indistinguishable** on an account with
no servers registered, so a 200 here establishes nothing about the key. Two
things follow, and both are recorded as tests rather than as prose:

1. Any code branching on `401` to mean "bad MCP credential" is wrong for this
   service — a missing header is 422 and a wrong key is 200.
2. The credential question can only be settled against an account that actually
   has a server registered, or through the execute route. Until then, `client.py`
   is correct by construction (it sends exactly what the hosted bridge sends) but
   unconfirmed by observation.

Note the split: the reachability tests need only a base and a key. The tests
that would actually answer the question need a `LYZR_MCP_SERVER_ID` and skip
without one.

    LYZR_MCP_CLIENT_URL=https://mcp-dev.test.studio.lyzr.ai \\
    LYZR_API_KEY=... \\
    LYZR_MCP_SERVER_ID=srv_...          # only for the execute tests
      uv run pytest -q -m integration tests/test_mcp_integration.py
"""

from __future__ import annotations

import os

import pytest

from computeragent.mcp.binding import McpBinding
from computeragent.mcp.client import LyzrMcpClient, McpServiceError
from computeragent.mcp.server import build_mcp_servers

pytestmark = pytest.mark.integration

BASE = os.environ.get("LYZR_MCP_CLIENT_URL")
KEY = os.environ.get("LYZR_API_KEY")
SERVER_ID = os.environ.get("LYZR_MCP_SERVER_ID")

needs_auth = pytest.mark.skipif(
    not (BASE and KEY), reason="set LYZR_MCP_CLIENT_URL and LYZR_API_KEY"
)
needs_service = pytest.mark.skipif(
    not (BASE and KEY and SERVER_ID),
    reason="set LYZR_MCP_CLIENT_URL, LYZR_API_KEY and LYZR_MCP_SERVER_ID",
)


@pytest.fixture
def client():
    return LyzrMcpClient(base_url=BASE or "", api_key=KEY or "")


@needs_auth
async def test_the_workspace_key_is_not_rejected_by_the_facade():
    """Necessary but NOT sufficient, and the difference matters.

    A 401/403 would settle the question negatively. A 200 settles nothing —
    see the test below for why — so this asserts only what it can: the SDK's
    request shape reaches the service and is not refused.
    """
    import httpx

    async with httpx.AsyncClient(timeout=25) as http:
        resp = await http.get(f"{BASE}/api/v1/mcp/servers", headers={"x-api-key": KEY})

    assert resp.status_code not in (401, 403), (
        f"the workspace key was REJECTED by {BASE} ({resp.status_code}). The SDK sends exactly "
        "what the hosted bridge sends (x-api-key, verbatim), so this is about the credential, "
        "not the request — check which key AgentOS stamps into mcpServers per dispatch."
    )
    assert isinstance(resp.json().get("servers"), list)


@needs_auth
async def test_the_list_route_is_not_an_auth_oracle():
    """Pins the reason the question above stays open.

    A deliberately invalid key gets the same 200 and the same empty list as a
    real one, so "it returned 200" can never be evidence that a credential
    works. If this test ever fails because the service started rejecting bad
    keys, then the test above becomes meaningful and the open question closes —
    which is exactly when someone should notice.
    """
    import httpx

    async with httpx.AsyncClient(timeout=25) as http:
        bogus = await http.get(
            f"{BASE}/api/v1/mcp/servers", headers={"x-api-key": "definitely_not_a_real_key"}
        )

    assert bogus.status_code == 200, (
        f"{BASE} now rejects an invalid key ({bogus.status_code}) — good. The list route has "
        "become a usable auth oracle, so the credential question can now be answered here and "
        "this test should be replaced by a real assertion."
    )
    assert bogus.json().get("servers") == []


@needs_auth
async def test_a_missing_header_is_refused_but_not_with_401():
    """Recorded because it is a trap: this service answers a missing credential
    with FastAPI's 422, not 401. Code that treats 401 as "bad MCP credential"
    will never fire here."""
    import httpx

    async with httpx.AsyncClient(timeout=25) as http:
        resp = await http.get(f"{BASE}/api/v1/mcp/servers")

    assert resp.status_code == 422
    assert "x-api-key" in resp.text


@needs_service
async def test_the_facade_returns_tools_in_the_documented_shape(client):
    """`mcp.ts` asserts the whole list comes back with no cursor, and that the
    schema arrives as snake_case `input_schema`. Both are assertions about
    today's service, not a contract — so they are checked here rather than
    assumed in the client."""
    tools = await client.list_tools(SERVER_ID)

    if not tools:
        pytest.skip(f"{SERVER_ID} exposes no tools — nothing to check the shape of")
    for t in tools:
        assert t.name
        assert isinstance(t.input_schema, dict)
        assert t.input_schema.get("type") == "object"


@needs_service
async def test_a_real_tool_executes_and_normalises(client):
    """End to end against the real service: discovery → execute → content the
    in-process path keeps. Uses the first tool with an empty-ish schema so it can
    be called with no arguments; skips when none qualifies."""
    tools = await client.list_tools(SERVER_ID)
    callable_ones = [t for t in tools if not (t.input_schema.get("required") or [])]
    if not callable_ones:
        pytest.skip("every tool on this server requires arguments — pick one by hand to test")

    spec = callable_ones[0]
    binding = McpBinding(
        server_id=SERVER_ID, action_names=(spec.name,), tool_name="Live", persist_auth=False
    )
    built = await build_mcp_servers((binding,), client)

    assert list(built.servers) == ["Live"]
    (tool,) = built.tools["Live"]
    out = await tool.handler({})

    # Either outcome is a pass — a tool that legitimately fails still proves the
    # path. What must NOT happen is an unhandled raise or a malformed result.
    assert isinstance(out["content"], list)
    assert all(b["type"] in {"text", "image", "resource_link", "resource"} for b in out["content"])
    assert isinstance(out["is_error"], bool)


@needs_service
async def test_an_unknown_server_id_fails_loudly(client):
    with pytest.raises(McpServiceError):
        await client.list_tools("srv_definitely_not_real_000")
