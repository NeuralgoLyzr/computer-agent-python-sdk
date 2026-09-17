"""MCP propagation onto worker sessions — the policy, the in-process skip, and
the allowlist rule that cost a wave once already."""

from __future__ import annotations

import logging

import pytest

from computeragent.orchestration.worker_mcp import inherit_mcp_into, resolve_worker_mcp

BRIDGE = {"type": "http", "url": "https://bridge/mcp/srv?tools=alpha,beta"}
PLAIN = {"type": "http", "url": "https://bridge/mcp/plain"}
IN_PROCESS = {"type": "sdk", "name": "gap_tools", "instance": object()}

PARENT = {"mcp_servers": {"jira": BRIDGE, "gap_tools": IN_PROCESS}}


# ── resolve_worker_mcp ─────────────────────────────────────────────────────


def test_absent_config_means_inherit_not_none():
    # The whole reason to read a policy through this function: "" would have
    # been an easy default and it is the wrong one.
    assert resolve_worker_mcp(None, None) == "inherit"


def test_the_crew_entry_overrides_the_orchestration_block():
    assert resolve_worker_mcp("none", "inherit") == "none"
    assert resolve_worker_mcp(None, "none") == "none"
    assert resolve_worker_mcp(("a",), "none") == ("a",)


# ── inherit_mcp_into ───────────────────────────────────────────────────────


def test_none_policy_returns_the_same_object_untouched():
    child: dict = {}
    assert inherit_mcp_into(child, PARENT, "none", "claude-agent-sdk") is child


def test_a_parent_with_no_servers_returns_the_same_object():
    child: dict = {}
    assert inherit_mcp_into(child, {}, "inherit", "claude-agent-sdk") is child


def test_inherit_copies_the_bridge_servers():
    out = inherit_mcp_into({}, PARENT, "inherit", "claude-agent-sdk")
    assert out["mcp_servers"] == {"jira": BRIDGE}


def test_an_in_process_server_is_never_inherited():
    # Its live instance is served by the session that built it, so a second
    # session presenting the same object cannot connect — and the inherited
    # entry would WIN over the worker's own working copy.
    out = inherit_mcp_into({}, PARENT, "inherit", "claude-agent-sdk")
    assert "gap_tools" not in out["mcp_servers"]


def test_a_named_list_selects_only_those_servers():
    parent = {"mcp_servers": {"jira": BRIDGE, "slack": PLAIN}}
    out = inherit_mcp_into({}, parent, ("slack",), "claude-agent-sdk")
    assert set(out["mcp_servers"]) == {"slack"}


def test_an_unknown_name_costs_that_tool_not_the_wave(caplog):
    with caplog.at_level(logging.WARNING, logger="computeragent.orchestration.worker_mcp"):
        out = inherit_mcp_into({}, PARENT, ("jira", "nope"), "claude-agent-sdk")
    assert set(out["mcp_servers"]) == {"jira"}
    assert "worker_mcp.unknown_server" in caplog.text


def test_an_inherited_entry_wins_over_a_repo_declared_one_of_the_same_name():
    child = {"mcp_servers": {"jira": {"type": "http", "url": "https://repo/declared"}}}
    out = inherit_mcp_into(child, PARENT, "inherit", "claude-agent-sdk")
    assert out["mcp_servers"]["jira"] == BRIDGE


# ── the allowlist rule ─────────────────────────────────────────────────────


def test_an_allowlist_is_never_created_where_none_existed():
    # Creating one flips "every tool allowed" into "only these MCP tools",
    # stripping the worker of Read/Write/Bash.
    out = inherit_mcp_into({}, PARENT, "inherit", "claude-agent-sdk")
    assert "allowed_tools" not in out


def test_an_existing_allowlist_is_extended_with_a_server_level_grant():
    out = inherit_mcp_into({"allowed_tools": ["Read"]}, PARENT, "inherit", "claude-agent-sdk")
    assert out["allowed_tools"] == ["Read", "mcp__jira"]


def test_extension_happens_for_gitclaw_too_using_per_tool_names():
    # A roster's `tools` list is model-authored; leaving it alone let the model
    # silently revoke a grant the wave's configuration made.
    out = inherit_mcp_into({"allowed_tools": ["read"]}, PARENT, "inherit", "gitagent")
    assert out["allowed_tools"] == ["read", "jira__alpha", "jira__beta"]


def test_gitclaw_says_so_when_an_entry_carries_no_tools_binding(caplog):
    parent = {"mcp_servers": {"plain": PLAIN}}
    with caplog.at_level(logging.WARNING, logger="computeragent.orchestration.worker_mcp"):
        out = inherit_mcp_into({"allowed_tools": ["read"]}, parent, "inherit", "gitagent")
    assert out["allowed_tools"] == ["read"]
    assert "allowlist_unextendable" in caplog.text


def test_a_grant_already_present_is_not_duplicated():
    out = inherit_mcp_into({"allowed_tools": ["mcp__jira"]}, PARENT, "inherit", "claude-agent-sdk")
    assert out["allowed_tools"] == ["mcp__jira"]


@pytest.mark.parametrize("engine", ["claude-agent-sdk", "claude-code", "deepagents"])
def test_every_anthropic_shaped_engine_gets_the_server_level_form(engine):
    out = inherit_mcp_into({"allowed_tools": []}, PARENT, "inherit", engine)
    assert out["allowed_tools"] == ["mcp__jira"]
