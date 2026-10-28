"""The Lyzr MCP facade client — transport, caching, and schema hygiene.

Cache semantics are ported from the TS bridge (`mcp.ts:210-248`) and each has a
reason a comment records: keying on the credential, and filtering *after* the
cache, are both about one agent's binding not affecting another's.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from computeragent.mcp.client import LyzrMcpClient, McpServiceError

BASE = "https://mcp.example.invalid"
KEY = "ags_secret_do_not_leak"
SRV = "srv_1"
TOOLS_URL = f"{BASE}/api/v1/mcp/servers/{SRV}/tools"
EXEC_URL = f"{BASE}/api/v1/mcp/tools/execute"

TOOLS = {
    "tools": [
        {
            "name": "get_weather",
            "description": "Weather for a city",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
        {"name": "list_cities", "input_schema": {"type": "object", "properties": {}}},
    ]
}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def client(**kw):
    kw.setdefault("base_url", BASE)
    kw.setdefault("api_key", KEY)
    return LyzrMcpClient(**kw)


# ── list_tools ───────────────────────────────────────────────────────────────


@respx.mock
async def test_lists_tools_and_carries_the_declared_schema():
    respx.get(TOOLS_URL).mock(return_value=httpx.Response(200, json=TOOLS))

    tools = await client().list_tools(SRV)

    assert [t.name for t in tools] == ["get_weather", "list_cities"]
    assert tools[0].description == "Weather for a city"
    assert tools[0].input_schema["properties"] == {"city": {"type": "string"}}


@respx.mock
async def test_a_server_id_is_url_encoded():
    route = respx.get(f"{BASE}/api/v1/mcp/servers/srv%2Fodd/tools").mock(
        return_value=httpx.Response(200, json={"tools": []})
    )

    await client().list_tools("srv/odd")

    assert route.called


@respx.mock
async def test_the_allowlist_filters_the_list():
    respx.get(TOOLS_URL).mock(return_value=httpx.Response(200, json=TOOLS))

    tools = await client().list_tools(SRV, allowlist=("get_weather",))

    assert [t.name for t in tools] == ["get_weather"]


@respx.mock
async def test_an_empty_allowlist_means_everything():
    respx.get(TOOLS_URL).mock(return_value=httpx.Response(200, json=TOOLS))

    assert len(await client().list_tools(SRV)) == 2


@respx.mock
async def test_a_trailing_slash_on_the_base_does_not_double_up():
    respx.get(TOOLS_URL).mock(return_value=httpx.Response(200, json={"tools": []}))

    await client(base_url=f"{BASE}/").list_tools(SRV)


# ── cache ────────────────────────────────────────────────────────────────────


@respx.mock
async def test_a_second_call_is_served_from_cache():
    route = respx.get(TOOLS_URL).mock(return_value=httpx.Response(200, json=TOOLS))
    c = client(now=Clock())

    await c.list_tools(SRV)
    await c.list_tools(SRV)

    assert route.call_count == 1


@respx.mock
async def test_the_cache_expires():
    route = respx.get(TOOLS_URL).mock(return_value=httpx.Response(200, json=TOOLS))
    clock = Clock()
    c = client(now=clock, tools_ttl_s=60)

    await c.list_tools(SRV)
    clock.t += 61
    await c.list_tools(SRV)

    assert route.call_count == 2


@respx.mock
async def test_two_credentials_do_not_share_a_cache_entry():
    """What a server exposes can differ per caller (persist_auth, per-user
    OAuth), so one caller's list must never be served to another."""
    route = respx.get(TOOLS_URL).mock(return_value=httpx.Response(200, json=TOOLS))

    await client(api_key="key_a").list_tools(SRV)
    await client(api_key="key_b").list_tools(SRV)

    assert route.call_count == 2


@respx.mock
async def test_a_narrow_binding_does_not_poison_the_cache_for_a_wider_one():
    """Filtering happens after caching for exactly this reason — two agents can
    be bound to the same server with different allowlists."""
    route = respx.get(TOOLS_URL).mock(return_value=httpx.Response(200, json=TOOLS))
    c = client(now=Clock())

    narrow = await c.list_tools(SRV, allowlist=("get_weather",))
    wide = await c.list_tools(SRV)

    assert route.call_count == 1
    assert len(narrow) == 1
    assert len(wide) == 2


# ── schema hygiene ───────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.parametrize(
    "schema",
    [
        None,
        {},
        {"type": "object"},  # no `properties` — the one that silently breaks
        {"properties": {"a": {}}},  # no `type`
        ["not", "a", "schema"],
        "object",
    ],
)
async def test_an_unusable_schema_falls_back_to_an_open_object(schema):
    """`_build_input_schema` keeps a dict verbatim only when it has a string
    `type` AND `properties`; anything else it reinterprets as a
    {param: python_type} map, after which every call fails jsonschema
    validation before the handler runs — a tool that is visible and
    permanently broken."""
    respx.get(TOOLS_URL).mock(
        return_value=httpx.Response(200, json={"tools": [{"name": "t", "input_schema": schema}]})
    )

    (tool,) = await client().list_tools(SRV)

    assert tool.input_schema == {"type": "object", "properties": {}}


@respx.mock
async def test_a_well_formed_schema_is_passed_through_verbatim():
    """It is what the remote server declares, so it is what the arguments should
    be validated against."""
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    }
    respx.get(TOOLS_URL).mock(
        return_value=httpx.Response(200, json={"tools": [{"name": "t", "input_schema": schema}]})
    )

    (tool,) = await client().list_tools(SRV)

    assert tool.input_schema == schema


# ── execute ──────────────────────────────────────────────────────────────────


@respx.mock
async def test_execute_posts_the_facade_shape():
    route = respx.post(EXEC_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "result": "ok"})
    )

    out = await client().execute(SRV, "get_weather", {"city": "Pune"})

    assert out == {"success": True, "result": "ok"}
    assert json.loads(route.calls.last.request.read()) == {
        "server_id": "srv_1",
        "tool_name": "get_weather",
        "arguments": {"city": "Pune"},
    }


@respx.mock
async def test_execute_returns_a_failure_envelope_rather_than_raising():
    """`success: false` is a TOOL-level outcome — the caller turns it into an
    is_error result the model can read and work around."""
    respx.post(EXEC_URL).mock(
        return_value=httpx.Response(200, json={"success": False, "error": "upstream 500"})
    )

    out = await client().execute(SRV, "t", {})

    assert out["success"] is False


# ── credential ───────────────────────────────────────────────────────────────


@respx.mock
async def test_the_credential_travels_as_x_api_key():
    route = respx.get(TOOLS_URL).mock(return_value=httpx.Response(200, json={"tools": []}))

    await client().list_tools(SRV)

    headers = route.calls.last.request.headers
    assert headers["x-api-key"] == KEY
    assert "authorization" not in headers


# ── failures ─────────────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 503])
async def test_a_non_2xx_raises_a_typed_error_carrying_the_status(status):
    respx.get(TOOLS_URL).mock(return_value=httpx.Response(status, text="denied"))

    with pytest.raises(McpServiceError, match=str(status)):
        await client().list_tools(SRV)


@respx.mock
async def test_a_transport_failure_is_wrapped_not_a_bare_httpx_traceback():
    respx.get(TOOLS_URL).mock(side_effect=httpx.ConnectError("nope"))

    with pytest.raises(McpServiceError, match="could not reach"):
        await client().list_tools(SRV)


@respx.mock
async def test_a_non_json_body_is_wrapped():
    respx.get(TOOLS_URL).mock(return_value=httpx.Response(200, text="<html>"))

    with pytest.raises(McpServiceError, match="non-JSON"):
        await client().list_tools(SRV)


@respx.mock
@pytest.mark.parametrize(
    "failure",
    [
        {"return_value": httpx.Response(401, text="bad key")},
        {"side_effect": httpx.ConnectError("nope")},
        {"return_value": httpx.Response(200, text="<html>")},
    ],
)
async def test_no_failure_path_leaks_the_credential(failure):
    """These messages are shown to users and written to logs."""
    respx.get(TOOLS_URL).mock(**failure)

    with pytest.raises(McpServiceError) as excinfo:
        await client().list_tools(SRV)

    assert KEY not in str(excinfo.value)


def test_the_repr_does_not_carry_the_credential():
    assert KEY not in repr(client())
