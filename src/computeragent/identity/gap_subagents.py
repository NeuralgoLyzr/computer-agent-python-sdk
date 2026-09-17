"""GAP sub-agents translator — Python port of the TS ``subagents.ts``.

Reads ``agents/`` from a GAP repo and classifies each entry by LOCALITY:

EMBEDDED
    ``agents/<name>.yaml`` with an inline ``prompt``, or a nested
    ``agents/<name>/{agent.yaml,SOUL.md}`` → a native Claude Agent SDK sub-agent
    (``AgentDefinition``), run in-context via the Agent tool.

REFERENCED
    ``agents/<name>.yaml`` with a ``use:`` pointing at a DIFFERENT repo /
    registered agent id → a WORKER that runs as its OWN session. A
    ``separate_session`` boolean decides its telemetry identity (own session id
    vs. folding under the orchestrator's). Referenced entries never become
    native sub-agents.

:func:`load_gap_subagents` returns only the native map (what the loader feeds
into ``ClaudeAgentOptions.agents``), unchanged. :func:`classify_gap_agents`
returns both the native map AND the worker crew, for the orchestration layer.

Referenced-file schema::

    use: <git url[@ref] or registered agent id>   # required
    separate_session?: <bool>                      # default true (own session)
    scope?: { write?: [glob...], read?: [glob...] }
    budget_usd?: <number>
    max_turns?: <int>
    model?: <alias or id>
    tools?: [<tool names>]
    mcp?: inherit | none | [<server names>]        # default: the orchestration
                                                   # block's worker_mcp, else inherit

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk.types import AgentDefinition

from ..orchestration.contract import WorkerCrewEntry, WorkerScope, normalize_mcp_policy

logger = logging.getLogger("computeragent.identity.gap")

_NESTED_SOUL = "SOUL.md"
_NESTED_MANIFEST = "agent.yaml"


@dataclass(frozen=True)
class GapAgents:
    """The classified crew of a GAP repo's ``agents/`` directory."""

    #: Embedded agents → native in-context sub-agents (the SDK ``agents`` map).
    native_subagents: dict[str, AgentDefinition] = field(default_factory=dict)
    #: Referenced (``use:``) agents → workers that run as their own sessions.
    workers: tuple[WorkerCrewEntry, ...] = ()


def classify_gap_agents(workdir: Path) -> GapAgents:
    """Classify every entry in ``agents/`` into native sub-agents vs. referenced
    workers.

    Non-``agents/`` repos return empty. Malformed / unrecognised entries are
    skipped (never raises).
    """
    agents_dir = workdir / "agents"
    try:
        entries = sorted(agents_dir.iterdir())
    except OSError:
        return GapAgents()

    native: dict[str, AgentDefinition] = {}
    workers: list[WorkerCrewEntry] = []

    for entry in entries:
        if entry.is_dir():
            # Nested dirs are embedded (SOUL.md-backed) → native sub-agents.
            definition = _load_nested(entry)
            if definition is not None:
                native[entry.name] = definition
            continue
        if entry.is_file() and entry.suffix in (".yaml", ".yml"):
            raw = _parse_yaml_file(entry)
            if not isinstance(raw, dict):
                continue
            # `use:` wins → a referenced worker (its own session), never a
            # native sub-agent.
            crew = _to_worker_entry(entry.stem, raw)
            if crew is not None:
                workers.append(crew)
                continue
            definition = _to_inline_def(raw)
            if definition is not None:
                native[entry.stem] = definition
    return GapAgents(native_subagents=native, workers=tuple(workers))


def load_gap_subagents(workdir: Path) -> dict[str, AgentDefinition]:
    """Backwards-compatible native-only view: ``agents/`` → the SDK subagent map.

    A repo with no ``use:`` entries (every repo today) gets the exact same
    result as before.
    """
    return classify_gap_agents(workdir).native_subagents


def _to_worker_entry(name: str, raw: dict[str, Any]) -> WorkerCrewEntry | None:
    """A ``use:``-referenced worker file: points at a different repo, runs its
    own session. ``None`` when this file is not one."""
    use = raw.get("use")
    if not (isinstance(use, str) and use.strip()):
        return None

    scope_block = raw.get("scope")
    write = _string_list(scope_block.get("write")) if isinstance(scope_block, dict) else None
    read = _string_list(scope_block.get("read")) if isinstance(scope_block, dict) else None
    scope = (
        WorkerScope(write=tuple(write) if write else None, read=tuple(read) if read else None)
        if (write or read)
        else None
    )

    separate = raw.get("separate_session")
    tools = _string_list(raw.get("tools"))
    return WorkerCrewEntry(
        name=name,
        use=use,
        # Referenced workers default to their OWN session (the fork model).
        separate_session=separate if isinstance(separate, bool) else True,
        scope=scope,
        budget_usd=_non_negative_number(raw.get("budget_usd")),
        max_turns=_positive_int(raw.get("max_turns")),
        model=raw["model"] if isinstance(raw.get("model"), str) else None,
        tool_subset=tuple(tools) if tools is not None else None,
        # Which of the ORCHESTRATOR's MCP servers this worker gets. Absent ⇒ the
        # orchestration block's `worker_mcp` decides (which itself defaults to
        # inheriting all of them).
        mcp=normalize_mcp_policy(raw.get("mcp")),
    )


def _non_negative_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _to_inline_def(raw: dict[str, Any]) -> AgentDefinition | None:
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


__all__ = ["GapAgents", "classify_gap_agents", "load_gap_subagents"]
