"""Parsing `mcp_servers` / `mcp_mode` — parity with `mcp-binding.ts:55-165`.

Every rule here exists because Lyzr Studio validates nothing about a tool
config: a typo'd server id, a misspelled action, an empty action list all
persist cleanly and then fail — or silently do nothing — much later. These
tests pin the conversion of each of those into an immediate, named refusal.
"""

from __future__ import annotations

import pytest

from computeragent.mcp.binding import (
    DEFAULT_MCP_MODE,
    McpBindingError,
    parse_mcp_bindings,
    parse_mcp_mode,
)


def one(**over):
    entry = {"server_id": "srv_1", "action_names": ["get_weather"]}
    entry.update(over)
    return {"mcp_servers": [entry]}


# ── mcp_servers: shape ───────────────────────────────────────────────────────


def test_absent_is_no_bindings():
    assert parse_mcp_bindings({}) == ()


def test_explicit_empty_is_no_bindings():
    """The TS distinguishes absent from `[]` because a PATCH must tell "leave it
    alone" from "clear it". This SDK only reads, so both mean the same here."""
    assert parse_mcp_bindings({"mcp_servers": []}) == ()


def test_a_non_list_is_refused():
    with pytest.raises(McpBindingError, match="must be an array"):
        parse_mcp_bindings({"mcp_servers": "srv_1"})


def test_a_non_object_entry_is_refused():
    with pytest.raises(McpBindingError, match="must be an object"):
        parse_mcp_bindings({"mcp_servers": ["srv_1"]})


@pytest.mark.parametrize("given", [{}, {"server_id": ""}, {"server_id": "   "}, {"server_id": 7}])
def test_a_missing_or_blank_server_id_is_refused(given):
    with pytest.raises(McpBindingError, match="requires a server_id"):
        parse_mcp_bindings({"mcp_servers": [{**given, "action_names": ["x"]}]})


@pytest.mark.parametrize("actions", ["x", {"a": 1}, ["x", 3], [None]])
def test_action_names_must_be_a_list_of_strings(actions):
    with pytest.raises(McpBindingError, match="array of strings"):
        parse_mcp_bindings(one(action_names=actions))


# ── the two rules that exist because the failure is otherwise invisible ──────


@pytest.mark.parametrize("actions", [[], ["", "   "]])
def test_an_empty_action_list_is_refused(actions):
    """Studio accepts an empty whitelist and gives the agent zero MCP tools
    without complaint — indistinguishable from a working binding until someone
    notices the agent never calls anything."""
    with pytest.raises(McpBindingError, match="silently exposes no tools") as excinfo:
        parse_mcp_bindings(one(action_names=actions))

    assert excinfo.value.code == "MCP_ACTIONS_REQUIRED"


def test_the_same_action_in_two_bindings_is_refused():
    """Lyzr keeps the FIRST occurrence and strips the rest, and a tool_config
    that loses all its actions that way is dropped from the stored agent with no
    error at all."""
    doc = {
        "mcp_servers": [
            {"server_id": "srv_a", "action_names": ["search"]},
            {"server_id": "srv_b", "action_names": ["list", "search"]},
        ]
    }
    with pytest.raises(McpBindingError, match="search") as excinfo:
        parse_mcp_bindings(doc)

    assert excinfo.value.code == "MCP_DUPLICATE_ACTION"
    # Naming only one of them would send the reader to the wrong binding.
    assert "srv_a" in str(excinfo.value)
    assert "srv_b" in str(excinfo.value)


def test_the_same_action_twice_in_ONE_binding_is_also_refused():
    """The TS keeps one `seenActions` map across the whole loop, so a repeat
    inside a single binding trips the same rule. Faithful, and correct: Lyzr
    dedupes it silently either way."""
    with pytest.raises(McpBindingError, match="listed twice") as excinfo:
        parse_mcp_bindings(one(action_names=["search", "search"]))

    assert excinfo.value.code == "MCP_DUPLICATE_ACTION"


# ── fields ───────────────────────────────────────────────────────────────────


def test_tool_name_defaults_to_the_server_id():
    """It is the tool NAMESPACE, so it must be stable and distinct per server;
    defaulting to the server id is what guarantees both."""
    (binding,) = parse_mcp_bindings(one())

    assert binding.tool_name == "srv_1"


@pytest.mark.parametrize("given", ["", "   ", 42, None])
def test_an_unusable_tool_name_falls_back_to_the_server_id(given):
    (binding,) = parse_mcp_bindings(one(tool_name=given))

    assert binding.tool_name == "srv_1"


def test_a_real_tool_name_is_kept_and_trimmed():
    (binding,) = parse_mcp_bindings(one(tool_name="  Weather  "))

    assert binding.tool_name == "Weather"


def test_action_names_are_trimmed():
    (binding,) = parse_mcp_bindings(one(action_names=[" a ", "b"]))

    assert binding.action_names == ("a", "b")


@pytest.mark.parametrize(
    ("given", "expected"),
    [(True, True), (False, False), ("true", False), (1, False), (None, False)],
)
def test_persist_auth_is_strictly_true(given, expected):
    """Matches the TS `=== true`: a string "true" from a hand-written config
    must not silently enable persistence."""
    (binding,) = parse_mcp_bindings(one(persist_auth=given))

    assert binding.persist_auth is expected


def test_bindings_are_frozen():
    """They are carried on a frozen ResolvedAgent and read at boot; a mutable
    one would let a later caller change what the tools point at."""
    import dataclasses

    (binding,) = parse_mcp_bindings(one())

    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.server_id = "other"  # type: ignore[misc]


# ── mcp_mode ─────────────────────────────────────────────────────────────────


def test_absent_mode_defaults_to_engine():
    assert parse_mcp_mode({}) == DEFAULT_MCP_MODE == "engine"


@pytest.mark.parametrize("mode", ["engine", "studio"])
def test_the_two_real_modes_parse(mode):
    assert parse_mcp_mode({"mcp_mode": mode}) == mode


@pytest.mark.parametrize("mode", ["Engine", "STUDIO", "both", "", 1, True, []])
def test_an_unrecognised_mode_is_refused_not_defaulted(mode):
    """Silently treating "Engine" as "studio" would leave the agent's tools
    running in the wrong place — the exact class of quiet misconfiguration the
    mode field exists to eliminate."""
    with pytest.raises(McpBindingError, match="mcp_mode"):
        parse_mcp_mode({"mcp_mode": mode})
