"""GAP loader wiring — the full OpenGAP surface mapped onto claude-agent-sdk
options: preset prompt, sub-agents, tools, hooks, external MCP servers, the
skills mirror, gateway carry, and compliance hardening."""

from __future__ import annotations

from pathlib import Path

import pytest

from computeragent.identity.gap import GapIdentityLoader
from computeragent.identity.gap_tools import MCP_SERVER_NAME


def _write_full_gap_repo(root: Path) -> None:
    (root / "agent.yaml").write_text(
        "name: full-agent\n"
        "version: 1.2.3\n"
        "description: Full-surface GAP agent.\n"
        "gateway: team-gw\n"
        "model:\n  preferred: anthropic:claude-sonnet-4-5\n"
        "runtime:\n  max_turns: 12\n  budget_usd: 2.5\n"
        "tools: [Glob]\n"
        "mcp_servers:\n"
        "  docs:\n"
        "    url: https://mcp.example/docs\n"
        "    headers:\n"
        "      Authorization: Bearer ${GAP_TEST_MCP_TOKEN}\n"
        "compliance:\n"
        "  supervision:\n"
        "    human_in_the_loop: destructive\n"
    )
    (root / "SOUL.md").write_text("Be helpful.")
    (root / "RULES.md").write_text("Never guess.")

    agents = root / "agents"
    agents.mkdir()
    (agents / "helper.yaml").write_text(
        "description: Helper\nprompt: You help.\n"
    )

    tools = root / "tools"
    tools.mkdir()
    (tools / "reader.yaml").write_text(
        "name: reader\nimplementation:\n  builtin: Read\n"
    )
    (tools / "runner.yaml").write_text(
        "name: runner\nimplementation:\n  script:\n    command: echo\n"
    )

    hooks = root / "hooks"
    hooks.mkdir()
    (hooks / "hooks.yaml").write_text(
        "PreToolUse:\n  - command: './check.sh'\n"
    )

    skills = root / "skills"
    (skills / "web-design").mkdir(parents=True)
    (skills / "web-design" / "SKILL.md").write_text("# Web design skill")


@pytest.mark.asyncio
async def test_full_claude_options_wiring(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GAP_TEST_MCP_TOKEN", "tok-123")
    _write_full_gap_repo(tmp_path)

    manifest = await GapIdentityLoader().load(
        source=tmp_path, target_engine="claude-agent-sdk", workspace_path=tmp_path
    )
    opts = dict(manifest.options)

    # Preset system prompt with the TS-format append.
    sp = opts["system_prompt"]
    assert sp["type"] == "preset" and sp["preset"] == "claude_code"
    assert "# Identity\nname: full-agent\nversion: 1.2.3" in sp["append"]
    assert "# Soul\n\nBe helpful." in sp["append"]
    assert "# Rules\n\nNever guess." in sp["append"]

    # Model / runtime / gateway.
    assert opts["model"] == "claude-sonnet-4-5"  # provider prefix stripped
    assert opts["max_turns"] == 12
    assert opts["max_budget_usd"] == 2.5
    assert opts["model_gateway_slug"] == "team-gw"

    # Tools: manifest list + builtin alias + script tool + external server prefix.
    assert opts["allowed_tools"] == [
        "Glob",
        "Read",
        f"mcp__{MCP_SERVER_NAME}__runner",
        "mcp__docs",
    ]

    # MCP servers: in-process gap_tools + the external HTTP server with the
    # ${VAR} header substituted and alwaysLoad set.
    servers = opts["mcp_servers"]
    assert servers[MCP_SERVER_NAME]["type"] == "sdk"
    assert servers["docs"] == {
        "type": "http",
        "url": "https://mcp.example/docs",
        "headers": {"Authorization": "Bearer tok-123"},
        "alwaysLoad": True,
    }

    # Sub-agents + hooks present.
    assert set(opts["agents"]) == {"helper"}
    assert "PreToolUse" in opts["hooks"]

    # Skills mirrored for the CLI's project-settings discovery.
    assert (tmp_path / ".claude" / "skills" / "web-design" / "SKILL.md").is_file()

    # Compliance hardening: prompt-skipping modes forced back to default,
    # everything else untouched.
    assert manifest.harden is not None
    assert manifest.harden({"permission_mode": "bypassPermissions"})["permission_mode"] == "default"
    assert manifest.harden({"permission_mode": "dontAsk"})["permission_mode"] == "default"
    assert manifest.harden({"permission_mode": "plan"})["permission_mode"] == "plan"
    assert manifest.harden({})== {}


@pytest.mark.asyncio
async def test_minimal_repo_has_no_harden_and_no_extras(tmp_path: Path):
    (tmp_path / "agent.yaml").write_text("name: mini\nversion: 0.1.0\n")
    manifest = await GapIdentityLoader().load(
        source=tmp_path, target_engine="claude-agent-sdk", workspace_path=tmp_path
    )
    opts = dict(manifest.options)
    assert manifest.harden is None
    assert "agents" not in opts
    assert "hooks" not in opts
    assert "mcp_servers" not in opts
    assert "allowed_tools" not in opts
    assert opts["system_prompt"]["preset"] == "claude_code"


@pytest.mark.asyncio
async def test_gitagent_path_unchanged(tmp_path: Path):
    (tmp_path / "agent.yaml").write_text(
        "name: g\nversion: 0.1.0\nmodel:\n  preferred: openai:gpt-4o-mini\n"
    )
    (tmp_path / "SOUL.md").write_text("Soul text.")
    manifest = await GapIdentityLoader().load(
        source=tmp_path, target_engine="gitagent", workspace_path=tmp_path
    )
    opts = dict(manifest.options)
    # gitagent keeps the plain stitched string prompt + prefixed model.
    assert isinstance(opts["system_prompt"], str)
    assert "## SOUL" in opts["system_prompt"]
    assert opts["model"] == "openai:gpt-4o-mini"
    assert manifest.harden is None
