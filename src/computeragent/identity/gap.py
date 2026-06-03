"""``GapIdentityLoader`` — reads a GitAgentProtocol (GAP) agent definition
from the workspace and resolves it to an :class:`AgentManifest`.

GAP defines an agent as a git repo with a known layout:

* ``agent.yaml`` — required root manifest with name, version, model,
  runtime, tools, skills, compliance.
* ``SOUL.md`` — optional. The agent's purpose / persona. Appended to the
  system prompt.
* ``RULES.md`` — optional. Behavioural constraints. Appended to system prompt.
* ``AGENTS.md`` — optional. Multi-agent crew definitions (skipped in MVP).
* ``tools/*.yaml`` — optional. Per-tool YAML manifests (skipped in MVP —
  ``GitAgentEngine`` is MCP-only in 0.2.0).
* ``skills/``, ``hooks/``, ``memory/`` — optional. Deferred to 0.3.0.

This loader is the Python port of the TypeScript
``@open-gitagent/identity-gitagentprotocol`` package. MVP scope: parse the
manifest, stitch SOUL/RULES into the system prompt, populate
engine-specific options. Compliance hardening (kill switch, HITL
requirements), tool wrapping into MCP servers, and skill loading are
parked.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..protocol_types import AgentManifest, IdentitySource

logger = logging.getLogger("computeragent.identity.gap")

_MANIFEST_FILENAME = "agent.yaml"
_SOUL_FILENAME = "SOUL.md"
_RULES_FILENAME = "RULES.md"
_AGENTS_FILENAME = "AGENTS.md"
_MISSING_YAML_HINT = (
    "GapIdentityLoader requires the optional ``pyyaml`` package. "
    "Install with: pip install 'computer-agent-py[gap]' (or [gitagent])"
)


class GapIdentityLoader:
    """Identity loader for GitAgentProtocol-shaped repos."""

    name = "gap"

    async def load(
        self,
        *,
        source: IdentitySource,  # noqa: ARG002 - workspace is the source of truth
        target_engine: str,
        workspace_path: Path,
    ) -> AgentManifest:
        manifest_path = workspace_path / _MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise FileNotFoundError(
                f"GAP manifest not found at {manifest_path}. "
                "Expected agent.yaml at the workspace root."
            )
        raw = _load_yaml(manifest_path)
        if not isinstance(raw, dict):
            raise ValueError(
                f"{_MANIFEST_FILENAME} must be a YAML mapping at the root; "
                f"got {type(raw).__name__}"
            )

        name = raw.get("name") or workspace_path.name or "gap-agent"
        version = raw.get("version") or "0.0.0"
        description = raw.get("description") or ""

        soul = _read_optional(workspace_path / _SOUL_FILENAME)
        rules = _read_optional(workspace_path / _RULES_FILENAME)
        agents = _read_optional(workspace_path / _AGENTS_FILENAME)

        system_prompt = _stitch_system_prompt(
            description=description, soul=soul, rules=rules, agents=agents
        )

        model_block = raw.get("model") or {}
        runtime_block = raw.get("runtime") or {}
        constraints = (
            (model_block.get("constraints") or {})
            if isinstance(model_block, dict)
            else {}
        )

        if target_engine == "gitagent":
            options = _gitagent_options(
                model_block=model_block,
                runtime_block=runtime_block,
                system_prompt=system_prompt,
                tool_names=list(raw.get("tools") or []),
                constraints=constraints,
            )
        elif target_engine == "claude-agent-sdk":
            options = _claude_agent_options(
                model_block=model_block,
                runtime_block=runtime_block,
                system_prompt=system_prompt,
                tool_names=list(raw.get("tools") or []),
            )
        else:
            # Unknown engine — pass a permissive options dict so the engine
            # can decide what to do.
            options = {
                "system_prompt": system_prompt,
                "model": _preferred_model(model_block),
                "max_turns": runtime_block.get("max_turns"),
                "tools": list(raw.get("tools") or []),
            }
            options = {k: v for k, v in options.items() if v is not None}

        logger.debug(
            "gap loader: name=%r version=%r target_engine=%r model=%r",
            name,
            version,
            target_engine,
            options.get("model"),
        )
        return AgentManifest(
            name=name,
            version=str(version),
            options=options,
        )


# ── helpers ────────────────────────────────────────────────────────────


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ImportError(_MISSING_YAML_HINT) from exc
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _read_optional(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("failed reading %s: %s", path, exc)
        return ""


def _stitch_system_prompt(
    *,
    description: str,
    soul: str,
    rules: str,
    agents: str,
) -> str:
    """Compose the agent's system prompt from the GAP files.

    Order matches the TS loader's stitching: identity (description) →
    SOUL → RULES → AGENTS. Empty sections are skipped, no separator noise.
    """
    sections: list[str] = []
    if description:
        sections.append(description.strip())
    if soul:
        sections.append("## SOUL\n\n" + soul.strip())
    if rules:
        sections.append("## RULES\n\n" + rules.strip())
    if agents:
        sections.append("## AGENTS\n\n" + agents.strip())
    return "\n\n".join(sections)


def _preferred_model(model_block: Any) -> str | None:
    if not isinstance(model_block, dict):
        return None
    return model_block.get("preferred")


def _gitagent_options(
    *,
    model_block: Any,
    runtime_block: dict[str, Any],
    system_prompt: str,
    tool_names: list[str],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    """Map GAP fields to ``GitAgentEngine``-shaped options."""
    out: dict[str, Any] = {
        "system_prompt": system_prompt,
        "model": _preferred_model(model_block),
    }
    if max_turns := runtime_block.get("max_turns"):
        out["max_turns"] = int(max_turns)
    if constraints:
        out["constraints"] = {
            k: v for k, v in constraints.items() if v is not None
        }
    if tool_names:
        # 0.2.0 gitagent treats this as a hint — actual tool execution
        # is deferred to 0.3.0. The engine's MCP-only stub will surface
        # the call back to the caller.
        out["tools_hint"] = tool_names
    return out


def _claude_agent_options(
    *,
    model_block: Any,
    runtime_block: dict[str, Any],
    system_prompt: str,
    tool_names: list[str],
) -> dict[str, Any]:
    """Map GAP fields to ``claude_agent_sdk.ClaudeAgentOptions`` kwargs.

    Strip the ``anthropic:`` prefix on model names since the SDK expects
    bare model ids.
    """
    raw_model = _preferred_model(model_block)
    model = _strip_provider_prefix(raw_model) if isinstance(raw_model, str) else None
    out: dict[str, Any] = {"system_prompt": system_prompt}
    if model:
        out["model"] = model
    if max_turns := runtime_block.get("max_turns"):
        out["max_turns"] = int(max_turns)
    if budget_usd := runtime_block.get("budget_usd"):
        out["max_budget_usd"] = float(budget_usd)
    if tool_names:
        # GAP tool names map straight to claude-agent-sdk allowed_tools.
        # Filesystem aliases (Read/Glob/Grep/etc) are recognised verbatim.
        out["allowed_tools"] = tool_names
    return out


def _strip_provider_prefix(model: str) -> str:
    """``anthropic:claude-...`` → ``claude-...``."""
    if ":" not in model:
        return model
    provider, _, rest = model.partition(":")
    if provider.lower() in {"anthropic", "openai", "bedrock", "lyzr"}:
        return rest
    return model


__all__ = ["GapIdentityLoader"]
