"""GAP sub-agents translator — Python port of the TS ``subagents.ts``.

Reads ``agents/`` from a GAP repo and produces the Claude Agent SDK's
``agents: dict[str, AgentDefinition]`` map, so the main agent can delegate to
them via the Agent tool.

Two on-disk layouts are honored (same as the TS loader):

``agents/<name>/agent.yaml`` + ``agents/<name>/SOUL.md``
    Full nested GAP shape. ``SOUL.md`` is required and becomes the
    sub-agent's prompt; ``agent.yaml`` (optional) supplies ``description``,
    ``model.preferred`` → ``model``, and ``tools``.

``agents/<name>.yaml``
    Inline single-file form::

        description: <required>
        prompt: <required — the sub-agent's system prompt>
        model: <optional model alias or id>
        tools: [<built-in or mcp__... tool names>]
        disallowed_tools: [...]

Sub-agent name is the directory/file basename. Invalid entries are silently
skipped (surfacing errors would couple the translator to a logging API — the
TS behavior). Missing ``agents/`` dir → ``{}``.

Out of scope (parity with TS): deeper nesting, ``tools/`` inside an
``agents/<n>/`` directory, per-sub-agent compliance blocks.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from claude_agent_sdk.types import AgentDefinition

logger = logging.getLogger("computeragent.identity.gap")

_NESTED_SOUL = "SOUL.md"
_NESTED_MANIFEST = "agent.yaml"


def load_gap_subagents(workdir: Path) -> dict[str, AgentDefinition]:
    """Read ``<workdir>/agents/`` into the SDK's subagent map."""
    agents_dir = workdir / "agents"
    try:
        entries = sorted(agents_dir.iterdir())
    except OSError:
        return {}

    out: dict[str, AgentDefinition] = {}
    for entry in entries:
        if entry.is_dir():
            definition = _load_nested(entry)
            if definition is not None:
                out[entry.name] = definition
        elif entry.is_file() and entry.suffix in (".yaml", ".yml"):
            definition = _load_inline(entry)
            if definition is not None:
                out[entry.stem] = definition
    return out


def _load_inline(path: Path) -> AgentDefinition | None:
    raw = _parse_yaml_file(path)
    if not isinstance(raw, dict):
        return None
    description = raw.get("description")
    prompt = raw.get("prompt")
    if not (isinstance(description, str) and description.strip()):
        return None
    if not (isinstance(prompt, str) and prompt.strip()):
        return None
    definition = AgentDefinition(description=description, prompt=prompt)
    model = raw.get("model")
    if isinstance(model, str) and model:
        definition.model = model
    tools = _string_list(raw.get("tools"))
    if tools is not None:
        definition.tools = tools
    disallowed = _string_list(raw.get("disallowed_tools"))
    if disallowed is not None:
        definition.disallowedTools = disallowed
    return definition


def _load_nested(directory: Path) -> AgentDefinition | None:
    # A sub-agent needs at minimum a prompt (SOUL.md).
    try:
        soul = (directory / _NESTED_SOUL).read_text(encoding="utf-8")
    except OSError:
        return None
    if not soul.strip():
        return None

    description = "Sub-agent"
    model: str | None = None
    tools: list[str] | None = None

    manifest = _parse_yaml_file(directory / _NESTED_MANIFEST)
    if isinstance(manifest, dict):
        if isinstance(manifest.get("description"), str) and manifest["description"]:
            description = manifest["description"]
        model_block = manifest.get("model")
        if isinstance(model_block, dict) and isinstance(model_block.get("preferred"), str):
            model = model_block["preferred"] or None
        tools = _string_list(manifest.get("tools"))

    definition = AgentDefinition(description=description, prompt=soul.strip())
    if model:
        definition.model = model
    if tools is not None:
        definition.tools = tools
    return definition


def _parse_yaml_file(path: Path) -> Any:
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except Exception:  # noqa: BLE001 — unreadable/malformed YAML → skip entry
        return None


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [v for v in value if isinstance(v, str)]


__all__ = ["load_gap_subagents"]
