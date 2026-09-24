"""GAP sub-agents loader — ``agents/`` → the SDK ``agents`` map."""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk.types import AgentDefinition

from computeragent.identity.gap_subagents import load_gap_subagents


def test_missing_agents_dir_yields_empty(tmp_path: Path):
    assert load_gap_subagents(tmp_path) == {}


def test_inline_yaml_subagent(tmp_path: Path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "reviewer.yaml").write_text(
        "description: Reviews diffs\n"
        "prompt: You review code changes.\n"
        "model: haiku\n"
        "tools: [Read, Grep]\n"
        "disallowed_tools: [Bash]\n"
    )
    out = load_gap_subagents(tmp_path)
    assert set(out) == {"reviewer"}
    d = out["reviewer"]
    assert isinstance(d, AgentDefinition)
    assert d.description == "Reviews diffs"
    assert d.prompt == "You review code changes."
    assert d.model == "haiku"
    assert d.tools == ["Read", "Grep"]
    assert d.disallowedTools == ["Bash"]


def test_inline_missing_required_fields_skipped(tmp_path: Path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "no-prompt.yaml").write_text("description: has no prompt\n")
    (agents / "no-desc.yml").write_text("prompt: has no description\n")
    (agents / "broken.yaml").write_text("{not yaml: [")
    assert load_gap_subagents(tmp_path) == {}


def test_nested_subagent_soul_required(tmp_path: Path):
    nested = tmp_path / "agents" / "researcher"
    nested.mkdir(parents=True)
    (nested / "SOUL.md").write_text("You research topics deeply.\n")
    (nested / "agent.yaml").write_text(
        "description: Research sub-agent\nmodel:\n  preferred: sonnet\ntools: [WebSearch]\n"
    )
    # A dir without SOUL.md is skipped.
    bare = tmp_path / "agents" / "empty"
    bare.mkdir()

    out = load_gap_subagents(tmp_path)
    assert set(out) == {"researcher"}
    d = out["researcher"]
    assert d.description == "Research sub-agent"
    assert d.prompt == "You research topics deeply."
    assert d.model == "sonnet"
    assert d.tools == ["WebSearch"]


def test_nested_defaults_without_manifest(tmp_path: Path):
    nested = tmp_path / "agents" / "minimal"
    nested.mkdir(parents=True)
    (nested / "SOUL.md").write_text("Minimal prompt.")
    out = load_gap_subagents(tmp_path)
    assert out["minimal"].description == "Sub-agent"
    assert out["minimal"].prompt == "Minimal prompt."
    assert out["minimal"].model is None
