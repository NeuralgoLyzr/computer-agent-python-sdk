"""Unit tests for the GAP identity loader.

We build a fake GAP workspace in a tmp dir and assert the loader maps
``agent.yaml`` + ``SOUL.md`` + ``RULES.md`` to engine-specific options
correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from computeragent.identity import GapIdentityLoader, resolve_identity_loader


def _write_gap_workspace(
    root: Path,
    *,
    name: str = "lyzr-docs-agent",
    version: str = "0.1.0",
    description: str = "Lyzr docs Q&A agent.",
    model_preferred: str = "anthropic:claude-sonnet-4-5-20250929",
    temperature: float | None = 0.2,
    max_turns: int | None = 30,
    tools: list[str] | None = None,
    soul: str | None = "You answer in concise sentences.",
    rules: str | None = "Cite docs when possible.",
) -> None:
    """Materialise a minimal GAP-shaped workspace."""
    constraints: dict = {}
    if temperature is not None:
        constraints["temperature"] = temperature
    model_block: dict = {"preferred": model_preferred}
    if constraints:
        model_block["constraints"] = constraints

    runtime_block: dict = {}
    if max_turns is not None:
        runtime_block["max_turns"] = max_turns

    manifest: dict = {
        "name": name,
        "version": version,
        "description": description,
        "model": model_block,
        "runtime": runtime_block,
    }
    if tools is not None:
        manifest["tools"] = tools

    # Hand-roll YAML to avoid coupling the test to a yaml-dump library.
    yaml_lines: list[str] = [
        f"name: {manifest['name']}",
        f"version: \"{manifest['version']}\"",
        f"description: |\n  {manifest['description']}",
        "model:",
        f"  preferred: \"{model_preferred}\"",
    ]
    if constraints:
        yaml_lines.append("  constraints:")
        for k, v in constraints.items():
            yaml_lines.append(f"    {k}: {v}")
    if runtime_block:
        yaml_lines.append("runtime:")
        for k, v in runtime_block.items():
            yaml_lines.append(f"  {k}: {v}")
    if tools is not None:
        yaml_lines.append("tools:")
        for tool in tools:
            yaml_lines.append(f"  - {tool}")
    (root / "agent.yaml").write_text("\n".join(yaml_lines) + "\n", encoding="utf-8")

    if soul is not None:
        (root / "SOUL.md").write_text(soul, encoding="utf-8")
    if rules is not None:
        (root / "RULES.md").write_text(rules, encoding="utf-8")


@pytest.mark.asyncio
async def test_gap_loader_gitagent_engine_mapping(tmp_path: Path):
    _write_gap_workspace(tmp_path, tools=["read", "search"])
    loader = GapIdentityLoader()

    manifest = await loader.load(
        source=tmp_path,
        target_engine="gitagent",
        workspace_path=tmp_path,
    )

    assert manifest.name == "lyzr-docs-agent"
    assert manifest.version == "0.1.0"
    opts = dict(manifest.options)
    # Gitagent keeps the provider-prefixed model (the OpenAI-compatible
    # gateway is expected to route on the prefix).
    assert opts["model"] == "anthropic:claude-sonnet-4-5-20250929"
    assert opts["max_turns"] == 30
    assert opts["constraints"] == {"temperature": 0.2}
    assert opts["tools_hint"] == ["read", "search"]
    sp = opts["system_prompt"]
    assert "Lyzr docs Q&A agent." in sp
    assert "You answer in concise sentences." in sp
    assert "Cite docs when possible." in sp


@pytest.mark.asyncio
async def test_gap_loader_claude_engine_strips_prefix(tmp_path: Path):
    _write_gap_workspace(tmp_path, tools=["Read", "Glob"])
    loader = GapIdentityLoader()

    manifest = await loader.load(
        source=tmp_path,
        target_engine="claude-agent-sdk",
        workspace_path=tmp_path,
    )

    opts = dict(manifest.options)
    # claude-agent-sdk expects bare model id (no provider prefix).
    assert opts["model"] == "claude-sonnet-4-5-20250929"
    assert opts["max_turns"] == 30
    assert opts["allowed_tools"] == ["Read", "Glob"]


@pytest.mark.asyncio
async def test_gap_loader_missing_manifest_raises(tmp_path: Path):
    # Empty workspace — no agent.yaml.
    loader = GapIdentityLoader()
    with pytest.raises(FileNotFoundError, match="agent.yaml"):
        await loader.load(
            source=tmp_path,
            target_engine="gitagent",
            workspace_path=tmp_path,
        )


@pytest.mark.asyncio
async def test_gap_loader_optional_files_absent(tmp_path: Path):
    """Manifest only — no SOUL/RULES/AGENTS. System prompt = description."""
    _write_gap_workspace(tmp_path, soul=None, rules=None)
    loader = GapIdentityLoader()
    manifest = await loader.load(
        source=tmp_path,
        target_engine="gitagent",
        workspace_path=tmp_path,
    )
    sp = dict(manifest.options)["system_prompt"]
    assert "Lyzr docs Q&A agent." in sp
    assert "SOUL" not in sp
    assert "RULES" not in sp


@pytest.mark.asyncio
async def test_gap_loader_registered_under_gap(tmp_path: Path):
    """The harness registry should resolve 'gap' to the GapIdentityLoader."""
    loader = resolve_identity_loader("gap")
    assert isinstance(loader, GapIdentityLoader)
    assert loader.name == "gap"


@pytest.mark.asyncio
async def test_gap_loader_unknown_engine_falls_through(tmp_path: Path):
    _write_gap_workspace(tmp_path, tools=["read"])
    loader = GapIdentityLoader()
    manifest = await loader.load(
        source=tmp_path,
        target_engine="some-future-engine",
        workspace_path=tmp_path,
    )
    opts = dict(manifest.options)
    assert opts["system_prompt"]
    assert opts["model"] == "anthropic:claude-sonnet-4-5-20250929"
    assert opts["max_turns"] == 30
    assert opts["tools"] == ["read"]
