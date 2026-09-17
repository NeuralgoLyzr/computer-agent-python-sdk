"""The hosted-bridge escape hatch — byte-for-byte parity with `applyMcpServers`.

The whole value of this path is that it is the SAME bridge the CAS uses, so any
divergence in the URL, the header or the grant defeats its purpose. These tests
pin the wire shape against `computeragent-server/src/index.ts:4270-4285`.
"""

from __future__ import annotations

import pytest

from computeragent.mcp.binding import McpBinding
from computeragent.mcp.bridge import build_bridge_servers

BRIDGE = "https://proxy.example.invalid"
KEY = "ags_test"


def binding(**over) -> McpBinding:
    kw = {
        "server_id": "srv_1",
        "action_names": ("get_weather", "list_cities"),
        "tool_name": "Weather",
        "persist_auth": False,
    }
    kw.update(over)
    return McpBinding(**kw)


def build(*bindings, **kw):
    kw.setdefault("bridge_url", BRIDGE)
    kw.setdefault("api_key", KEY)
    return build_bridge_servers(bindings or (binding(),), **kw)


def test_the_entry_matches_what_the_cas_emits():
    built = build()

    assert built.servers["Weather"] == {
        "type": "http",
        "url": f"{BRIDGE}/mcp/srv_1?tools=get_weather%2Clist_cities",
        "headers": {"x-api-key": KEY},
        "alwaysLoad": True,
    }


def test_the_grant_is_server_level():
    """One `mcp__<ns>` entry covers the whole server for an Anthropic-shaped
    engine — the CAS writes exactly this and nothing per-tool."""
    assert build().grants == ["mcp__Weather"]


def test_the_allowlist_rides_in_the_query_string():
    """That is what keeps the bridge stateless: it holds no per-agent binding of
    its own, so the URL carries the whole binding."""
    url = build().servers["Weather"]["url"]

    assert "?tools=" in url
    assert "get_weather" in url


def test_a_binding_with_no_actions_omits_the_query_entirely():
    """An empty `?tools=` would read as "allow nothing" rather than "allow all"
    — the bridge treats an absent query as unrestricted."""
    built = build(binding(action_names=()))

    assert built.servers["Weather"]["url"] == f"{BRIDGE}/mcp/srv_1"


def test_a_server_id_is_url_encoded():
    built = build(binding(server_id="srv/odd", tool_name="Odd"))

    assert built.servers["Odd"]["url"].startswith(f"{BRIDGE}/mcp/srv%2Fodd")


def test_a_trailing_slash_on_the_bridge_url_does_not_double_up():
    built = build_bridge_servers((binding(),), bridge_url=f"{BRIDGE}/", api_key=KEY)

    assert built.servers["Weather"]["url"].startswith(f"{BRIDGE}/mcp/")


def test_the_credential_is_the_only_authentication():
    """The bridge 401s a request presenting none, and has no operator token to
    fall back on — that fallback was removed precisely because it made the
    bridge an open relay."""
    assert build().servers["Weather"]["headers"] == {"x-api-key": KEY}


def test_no_network_call_is_made():
    """Unlike the in-process path there is no discovery step: the bridge does
    tools/list itself when the CLI handshakes. This must stay synchronous and
    offline, or construction acquires a network dependency."""
    import inspect

    assert not inspect.iscoroutinefunction(build_bridge_servers)


# ── the same namespace guards as the in-process path ─────────────────────────


@pytest.mark.parametrize("bad", ["My Server", "a.b", ""])
def test_an_unusable_namespace_is_skipped(bad):
    assert build(binding(tool_name=bad)).servers == {}


def test_a_reserved_namespace_is_skipped():
    assert build(binding(tool_name="gap_tools")).servers == {}


def test_a_duplicate_namespace_keeps_the_first():
    """The CAS would silently overwrite the key here; we do not."""
    built = build(binding(), binding(server_id="srv_2"))

    assert list(built.servers) == ["Weather"]
    assert built.servers["Weather"]["url"].startswith(f"{BRIDGE}/mcp/srv_1")


def test_two_distinct_bindings_both_land():
    built = build(binding(), binding(server_id="srv_2", tool_name="Docs", action_names=("read",)))

    assert sorted(built.servers) == ["Docs", "Weather"]
    assert sorted(built.grants) == ["mcp__Docs", "mcp__Weather"]
