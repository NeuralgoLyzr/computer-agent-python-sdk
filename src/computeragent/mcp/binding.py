"""Parse the ``mcp_servers`` / ``mcp_mode`` fields off an AgentOS agent document.

A port of ``parseMcpBindings`` / ``parseMcpMode``
(``agentos-server/src/mcp-binding.ts:55-165``). Every rule here exists because
Lyzr Studio validates **nothing** about an MCP tool config: a typo'd
``server_id``, a misspelled action name, or an empty action list all persist
cleanly and then fail — or silently do nothing — at the first inference. The TS
converts each of those late, opaque failures into an immediate 400; we convert
them into a refusal at construction, which is the same trade in a different
process.

The one rule that is NOT ported is the remote half (``validateMcpBindings``:
does the server exist, does every action name match a real tool). That is
fail-soft server-side and needs a credentialed round-trip; here the equivalent
signal is the discovery log at boot, which prints the requested action names
beside the server's real tool names.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Mode for an agent that doesn't state one — ``mcp-binding.ts:44``. ``engine``
#: because that is what MCP is for here: the agent acting mid-task needs the
#: tools in its own toolset so it can interleave them with Bash/Read/Write, and
#: every call then produces an ``execute_tool`` span and passes through the
#: policy decider.
DEFAULT_MCP_MODE = "engine"

MCP_MODES = frozenset({"engine", "studio"})


class McpBindingError(ValueError):
    """An ``mcp_servers`` / ``mcp_mode`` value this SDK will not run.

    Carries the TS error ``code`` where there is one, so a message can be
    grepped against the server's own behaviour.
    """

    def __init__(self, message: str, *, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class McpBinding:
    """One MCP server bound to an agent.

    ``tool_name`` is the tool **namespace**, not decoration: the engine exposes
    this server's tools as ``mcp__<tool_name>__<action>``. It defaults to
    ``server_id`` at bind time (``mcp-binding.ts:157-160``), which is what
    guarantees it is stable and distinct per server.
    """

    server_id: str
    action_names: tuple[str, ...]
    tool_name: str
    persist_auth: bool


def parse_mcp_mode(doc: Mapping[str, Any]) -> str:
    """``mcp_mode``, defaulted. An unrecognised value is an error.

    Never a silent fallback: treating ``"Engine"`` as ``"studio"`` would leave
    the agent's tools running in the wrong place, which is exactly the class of
    quiet misconfiguration this feature exists to eliminate
    (``mcp-binding.ts:46-67``).
    """
    raw = doc.get("mcp_mode")
    if raw is None:
        return DEFAULT_MCP_MODE
    if isinstance(raw, str) and raw in MCP_MODES:
        return raw
    return _fail(f'mcp_mode must be "studio" or "engine" (got {raw!r})')


def parse_mcp_bindings(doc: Mapping[str, Any]) -> tuple[McpBinding, ...]:
    """``mcp_servers`` → bindings, applying every rule checkable without a
    network call.

    Absent and ``[]`` both yield ``()``. The TS distinguishes them because a
    PATCH must tell "leave the stored config alone" from "explicitly clear it";
    this SDK only ever reads, so the distinction has no consumer here.
    """
    raw = doc.get("mcp_servers")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        return _fail(f"mcp_servers must be an array, got {type(raw).__name__}")

    bindings: list[McpBinding] = []
    # Cross-binding state. Lyzr's own `_dedupe_tool_config_actions` keeps the
    # FIRST occurrence of an action name and strips the rest — and a tool_config
    # that loses all of its actions that way is dropped from the stored agent
    # entirely, with no error. Two servers both exposing e.g. `search` therefore
    # cannot both be attached, and the failure is invisible.
    claimed_by: dict[str, str] = {}

    for entry in raw:
        if not isinstance(entry, Mapping):
            return _fail("each mcp_servers entry must be an object")

        raw_id = entry.get("server_id")
        server_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if not server_id:
            return _fail("each mcp_servers entry requires a server_id")

        actions_raw = entry.get("action_names")
        if not isinstance(actions_raw, list) or any(not isinstance(a, str) for a in actions_raw):
            return _fail(f"mcp_servers[{server_id}].action_names must be an array of strings")
        actions = tuple(a.strip() for a in actions_raw if a.strip())

        # Studio accepts an empty whitelist and gives the agent zero MCP tools
        # without complaint — indistinguishable from a working binding until
        # someone notices the agent never calls anything.
        if not actions:
            return _fail(
                f"mcp_servers[{server_id}] requires at least one action name — an empty list "
                "silently exposes no tools",
                code="MCP_ACTIONS_REQUIRED",
            )

        for action in actions:
            prior = claimed_by.get(action)
            if prior is not None:
                # Same refusal either way — the TS uses one map across the whole
                # loop, so a repeat inside a single binding trips it too. Only
                # the wording differs, because "claimed by both srv_1 and srv_1"
                # reads like a bug in the checker rather than in the config.
                where = (
                    f"listed twice in mcp_servers[{server_id}]"
                    if prior == server_id
                    else f"claimed by both {prior} and {server_id}"
                )
                return _fail(
                    f'action "{action}" is {where}; an action name may appear only once across '
                    "all MCP bindings (Lyzr keeps the first and silently drops the rest)",
                    code="MCP_DUPLICATE_ACTION",
                )
            claimed_by[action] = server_id

        raw_name = entry.get("tool_name")
        tool_name = (
            raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else server_id
        )

        bindings.append(
            McpBinding(
                server_id=server_id,
                action_names=actions,
                tool_name=tool_name,
                # `is True`, matching the TS `=== true`: a string "true" from a
                # hand-written config must not silently enable persistence.
                persist_auth=entry.get("persist_auth") is True,
            )
        )

    return tuple(bindings)


def _fail(message: str, *, code: str = "BAD_REQUEST") -> Any:
    raise McpBindingError(message, code=code)


__all__ = [
    "DEFAULT_MCP_MODE",
    "MCP_MODES",
    "McpBinding",
    "McpBindingError",
    "parse_mcp_bindings",
    "parse_mcp_mode",
]
