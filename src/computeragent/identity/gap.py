"""``GapIdentityLoader`` — reads a GitAgentProtocol (GAP) agent definition
from the workspace and resolves it to an :class:`AgentManifest`.

GAP defines an agent as a git repo with a known layout:

* ``agent.yaml`` — required root manifest with name, version, model,
  runtime, tools, mcp_servers, gateway, compliance.
* ``SOUL.md`` — optional. The agent's purpose / persona. Appended to the
  system prompt.
* ``RULES.md`` — optional. Behavioural constraints. Appended to system prompt.
* ``AGENTS.md`` — optional. Free-text crew notes, appended to system prompt.
* ``agents/`` — optional. Structured sub-agents → the SDK ``agents`` map
  (:mod:`.gap_subagents`), claude-agent-sdk engine only.
* ``tools/*.yaml`` — optional. Built-in aliases → ``allowed_tools``; script
  tools → the in-process ``gap_tools`` MCP server (:mod:`.gap_tools`),
  claude-agent-sdk engine only (``GitAgentEngine`` stays MCP-stub in 0.2.x).
* ``hooks/hooks.yaml`` — optional. Lifecycle hook commands → the SDK
  ``hooks`` option (:mod:`.gap_hooks`), claude-agent-sdk engine only.
* ``skills/`` — optional. Mirrored to ``.claude/skills/`` so the CLI's own
  project-settings discovery picks them up.

This loader is the Python port of the TypeScript
``@open-gitagent/identity-gitagentprotocol`` package, at parity with its
claude-agent-sdk adapter: manifest parse, prompt stitch (``claude_code``
preset + append), sub-agents, tools, hooks, external MCP servers, the
skills mirror, and compliance hardening (HITL → permission mode, applied
post-merge via :class:`AgentManifest`'s ``harden``).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..protocol_types import AgentManifest, IdentitySource
from .gap_hooks import load_gap_hooks
from .gap_subagents import load_gap_subagents
from .gap_tools import MCP_SERVER_NAME, load_gap_tools

logger = logging.getLogger("computeragent.identity.gap")

_MANIFEST_FILENAME = "agent.yaml"
_SOUL_FILENAME = "SOUL.md"
_RULES_FILENAME = "RULES.md"
_AGENTS_FILENAME = "AGENTS.md"
_MISSING_YAML_HINT = (
    "GapIdentityLoader requires the optional ``pyyaml`` package. "
    "Install with: pip install 'computer-agent-py[gap]' (or [gitagent])"
)
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


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
                f"{_MANIFEST_FILENAME} must be a YAML mapping at the root; got {type(raw).__name__}"
            )

        name = raw.get("name") or workspace_path.name or "gap-agent"
        version = raw.get("version") or "0.0.0"
        description = raw.get("description") or ""

        soul = _read_optional(workspace_path / _SOUL_FILENAME)
        rules = _read_optional(workspace_path / _RULES_FILENAME)
        agents_md = _read_optional(workspace_path / _AGENTS_FILENAME)

        model_block = raw.get("model") or {}
        runtime_block = raw.get("runtime") or {}
        constraints = (
            (model_block.get("constraints") or {}) if isinstance(model_block, dict) else {}
        )

        harden: Callable[[dict[str, Any]], dict[str, Any]] | None = None

        if target_engine == "gitagent":
            options = _gitagent_options(
                model_block=model_block,
                runtime_block=runtime_block,
                system_prompt=_stitch_system_prompt(
                    description=description, soul=soul, rules=rules, agents=agents_md
                ),
                tool_names=list(raw.get("tools") or []),
                constraints=constraints,
            )
        elif target_engine == "claude-agent-sdk":
            options = _claude_agent_options(
                raw=raw,
                name=str(name),
                version=str(version),
                description=description,
                soul=soul,
                rules=rules,
                agents_md=agents_md,
                model_block=model_block,
                runtime_block=runtime_block,
                workspace_path=workspace_path,
            )
            harden = _build_harden(raw)
        else:
            # Unknown engine — pass a permissive options dict so the engine
            # can decide what to do.
            options = {
                "system_prompt": _stitch_system_prompt(
                    description=description, soul=soul, rules=rules, agents=agents_md
                ),
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
            harden=harden,
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
    """Compose the agent's system prompt from the GAP files (gitagent path).

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


def _stitch_claude_append(
    *,
    name: str,
    version: str,
    description: str,
    soul: str,
    rules: str,
    agents_md: str,
) -> str:
    """The ``claude_code``-preset append, matching the TS adapter's format."""
    sections: list[str] = [f"# Identity\nname: {name}\nversion: {version}"]
    if description:
        sections.append(f"description: {description.strip()}")
    if soul:
        sections.append("# Soul\n\n" + soul.strip())
    if rules:
        sections.append("# Rules\n\n" + rules.strip())
    if agents_md:
        sections.append("# Agents\n\n" + agents_md.strip())
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
        out["constraints"] = {k: v for k, v in constraints.items() if v is not None}
    if tool_names:
        # 0.2.0 gitagent treats this as a hint — actual tool execution
        # is deferred to 0.3.0. The engine's MCP-only stub will surface
        # the call back to the caller.
        out["tools_hint"] = tool_names
    return out


def _claude_agent_options(
    *,
    raw: dict[str, Any],
    name: str,
    version: str,
    description: str,
    soul: str,
    rules: str,
    agents_md: str,
    model_block: Any,
    runtime_block: dict[str, Any],
    workspace_path: Path,
) -> dict[str, Any]:
    """Map the full GAP repo to ``claude_agent_sdk.ClaudeAgentOptions`` kwargs.

    At parity with the TS ``gapToClaudeAgentOptions``: preset system prompt,
    model/max_turns/budget, ``tools/`` (builtin aliases + script tools → the
    in-process ``gap_tools`` MCP server), external ``mcp_servers``, ``agents/``
    sub-agents, ``hooks/hooks.yaml``, and the ``skills/`` mirror.
    """
    out: dict[str, Any] = {
        "system_prompt": {
            "type": "preset",
            "preset": "claude_code",
            "append": _stitch_claude_append(
                name=name,
                version=version,
                description=description,
                soul=soul,
                rules=rules,
                agents_md=agents_md,
            ),
        }
    }

    raw_model = _preferred_model(model_block)
    model = _strip_provider_prefix(raw_model) if isinstance(raw_model, str) else None
    if model:
        out["model"] = model
    if max_turns := runtime_block.get("max_turns"):
        out["max_turns"] = int(max_turns)
    budget_usd = runtime_block.get("budget_usd")
    if budget_usd is not None:
        out["max_budget_usd"] = float(budget_usd)

    # Model-gateway binding — carried for downstream visibility/stamping
    # (parity with TS's flat ``modelGatewaySlug``). The engine's options
    # filter drops it before ``ClaudeAgentOptions`` construction; the server
    # owns gateway resolution.
    if isinstance(raw.get("gateway"), str) and raw["gateway"]:
        out["model_gateway_slug"] = raw["gateway"]

    # ── tools: agent.yaml `tools:` list + tools/*.yaml manifests ─────────
    allowed: list[str] = [t for t in (raw.get("tools") or []) if isinstance(t, str)]
    mcp_servers: dict[str, Any] = {}
    tools_result = load_gap_tools(workspace_path)
    allowed.extend(tools_result.allowed_tools)
    allowed.extend(tools_result.mcp_tool_names)
    if tools_result.mcp_server is not None:
        mcp_servers[MCP_SERVER_NAME] = tools_result.mcp_server

    # ── external MCP servers from agent.yaml (HTTP only) ─────────────────
    # `${VAR}` in header values is substituted from the environment; the
    # whole server's tools are allowed via the `mcp__<name>` prefix.
    # `alwaysLoad` keeps the tools in the turn-1 prompt (untyped extra key
    # on the Python TypedDict — forwarded verbatim).
    for server_name, cfg in (raw.get("mcp_servers") or {}).items():
        if not (isinstance(server_name, str) and isinstance(cfg, dict)):
            continue
        url = cfg.get("url")
        if not (isinstance(url, str) and url):
            continue
        headers = {str(k): _substitute_env(str(v)) for k, v in (cfg.get("headers") or {}).items()}
        mcp_servers[server_name] = {
            "type": "http",
            "url": url,
            "headers": headers,
            "alwaysLoad": True,
        }
        allowed.append(f"mcp__{server_name}")

    if allowed:
        out["allowed_tools"] = allowed
    if mcp_servers:
        out["mcp_servers"] = mcp_servers

    # ── sub-agents + hooks ───────────────────────────────────────────────
    agents = load_gap_subagents(workspace_path)
    if agents:
        out["agents"] = agents
    hooks = load_gap_hooks(workspace_path)
    if hooks:
        out["hooks"] = hooks

    # ── skills: mirror skills/ → .claude/skills/ for CLI discovery ───────
    _mirror_skills(workspace_path)

    return out


def _build_harden(raw: dict[str, Any]) -> Callable[[dict[str, Any]], dict[str, Any]] | None:
    """Compliance hardening: strictest-wins gate applied AFTER caller merge.

    ``compliance.supervision.human_in_the_loop`` ∈ {always, destructive} →
    a permission mode that never prompts (``bypassPermissions``, and the
    Python-only ``dontAsk``/``auto``) is forced back to ``default``.
    """
    compliance = raw.get("compliance")
    supervision = compliance.get("supervision") if isinstance(compliance, dict) else None
    hitl = supervision.get("human_in_the_loop") if isinstance(supervision, dict) else None
    if hitl not in ("always", "destructive"):
        return None

    def harden(options: dict[str, Any]) -> dict[str, Any]:
        if options.get("permission_mode") in ("bypassPermissions", "dontAsk", "auto"):
            return {**options, "permission_mode": "default"}
        return options

    return harden


def _mirror_skills(workspace_path: Path) -> None:
    """Copy ``skills/`` → ``.claude/skills/`` (port of the TS ``skills.ts``).

    The CLI's own project-settings discovery then picks the skills up —
    works because the engine sets ``cwd`` to the workspace and leaves
    ``setting_sources`` at the CLI default. No-op when absent; best-effort.
    """
    src = workspace_path / "skills"
    if not src.is_dir():
        return
    dest = workspace_path / ".claude" / "skills"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, dirs_exist_ok=True)
    except OSError as exc:
        logger.warning("skills mirror failed for %s: %s", src, exc)


def _substitute_env(value: str) -> str:
    """Replace ``${VAR}`` with ``os.environ[VAR]`` (empty string if unset)."""
    return _ENV_VAR_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _strip_provider_prefix(model: str) -> str:
    """``anthropic:claude-...`` → ``claude-...``."""
    if ":" not in model:
        return model
    provider, _, rest = model.partition(":")
    if provider.lower() in {"anthropic", "openai", "bedrock", "lyzr"}:
        return rest
    return model


__all__ = ["GapIdentityLoader"]
