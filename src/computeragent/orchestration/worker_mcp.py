"""Propagate an orchestrator's MCP servers onto its worker sessions.

WHY THIS EXISTS. MCP servers reach an agent as resolved ``options["mcp_servers"]``
on its ONE top-level session: AgentOS binds them and the SDK builds the
in-process (or bridge) entries. A worker is a sibling session created inside the
harness, long after that — so without this it inherits the orchestrator's
``envs`` but none of its tools, and a fan-out silently splits the tool surface
between the agent that plans and the agents that work.

WHY COPYING IS SAFE. Nothing in a materialised entry is session-scoped: a bridge
URL is built from configuration only, the credential is a bare ``x-api-key``
with no nonce or binding, and the bridge itself holds no per-session state (its
only cache is keyed ``server_id::credential``). A worker presenting the same
entry is indistinguishable upstream from the orchestrator presenting it.

.. warning::
   That credential is the CALLER's. A worker cloned from a third-party repo via
   ``use:`` therefore acts with the caller's MCP identity — which is why the
   spawn site logs the grant when the source is not inline.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("computeragent.orchestration.worker_mcp")

#: Engines that namespace MCP tools ``mcp__<server>__<tool>`` and read
#: ``mcp__<server>`` as a server-level grant.
_ANTHROPIC_SHAPED = frozenset({"claude-agent-sdk", "claude-code", "deepagents"})


def resolve_worker_mcp(entry: Any, spec: Any) -> Any:
    """The orchestration default when nothing says otherwise.

    Absent config means inherit — NOT ``"none"`` — so read a policy through here,
    never off the spec.
    """
    if entry is not None:
        return entry
    if spec is not None:
        return spec
    return "inherit"


def _mcp_servers_of(options: Any) -> dict[str, Any] | None:
    if not isinstance(options, dict):
        return None
    servers = options.get("mcp_servers")
    if not isinstance(servers, dict) or not servers:
        return None
    return servers


def _is_in_process(entry: Any) -> bool:
    """An IN-PROCESS SDK MCP server (``create_sdk_mcp_server``, ``type: "sdk"``)
    — the GAP loader's own ``gap_tools``, built from the repo's ``tools/*.yaml``.

    These must NOT be inherited. The entry holds a live ``instance`` served over
    the SDK's control protocol by the session that created it, so a second
    session presenting the same object cannot connect to it: the worker's
    ``system/init`` reports ``{name: "gap_tools", status: "failed"}`` while the
    orchestrator's says ``connected``. Worse, the inherited entry WINS over the
    worker's own (body over loader), so copying it replaces a ``gap_tools`` that
    would have worked — closed over the worker's own worktree — with one that
    cannot. Skipping leaves the worker's loader-built server in place, which is
    both functional and correctly scoped.
    """
    if isinstance(entry, dict):
        return entry.get("type") == "sdk"
    return getattr(entry, "type", None) == "sdk"


def _bound_tool_names_of(entry: Any) -> list[str]:
    """The ``?tools=a,b`` allowlist carried on a materialised bridge entry's URL."""
    url = entry.get("url") if isinstance(entry, dict) else getattr(entry, "url", None)
    if not isinstance(url, str):
        return []
    query = urlparse(url).query
    if not query:
        return []
    raw = parse_qs(query).get("tools", [""])[0]
    return [t.strip() for t in raw.split(",") if t.strip()]


def _grants_for(engine_name: str, server_name: str, entry: Any) -> list[str]:
    """The allowlist entries an engine needs for ``server_name`` to be CALLABLE.

    The two engines do not agree on either the namespace or the granularity:

    * Anthropic-shaped: tools are ``mcp__<server>__<tool>`` and ``mcp__<server>``
      is honoured as a server-LEVEL grant, so one entry covers the server.
    * gitclaw: tools are ``<server>__<tool>`` and ``allowed_tools`` is an
      exact-match set (no globs, no server-level form), so every tool has to be
      named individually.

    gitclaw's tool names come from the materialised bridge URL's ``?tools=``
    query, which is the authoritative per-agent binding AgentOS stamped at
    dispatch. Absent (⇒ "every tool this server exposes") there is nothing to
    enumerate, and the caller warns rather than guessing.
    """
    if engine_name in _ANTHROPIC_SHAPED:
        return [f"mcp__{server_name}"]
    return [f"{server_name}__{tool}" for tool in _bound_tool_names_of(entry)]


def inherit_mcp_into(
    child_options: dict[str, Any],
    parent_options: Any,
    policy: Any,
    engine_name: str,
) -> dict[str, Any]:
    """Fold the orchestrator's MCP servers into a worker's engine options per
    ``policy``.

    Returns ``child_options`` UNCHANGED (the same object) when there is nothing
    to add, so callers keep their existing "only pass options when non-empty"
    handling. Never raises — a tool-surface miss must not lose a worker.

    :param child_options: the worker's own options (from its crew/roster entry)
    :param parent_options: the orchestrator's merged + hardened engine options
    :param engine_name: the worker's engine, deciding whether ``allowed_tools``
        is Anthropic-shaped
    """
    if policy == "none":
        return child_options

    parent_servers = _mcp_servers_of(parent_options)
    if not parent_servers:
        return child_options

    names = list(parent_servers.keys()) if policy == "inherit" else list(policy or ())
    selected: dict[str, Any] = {}
    for name in names:
        if name not in parent_servers:
            # Skip, don't fail: a stale name in a crew file should cost that one
            # tool, not the whole wave. Silence here would be the bad outcome —
            # the worker would just behave as though the tool never existed.
            # (Only reachable for an explicit name list; `inherit` reads the keys.)
            logger.warning(
                "worker_mcp.unknown_server: %r is not one of the orchestrator's servers (%s)",
                name,
                ", ".join(sorted(parent_servers)),
            )
            continue
        entry = parent_servers[name]
        if _is_in_process(entry):
            # Logged at info, not warning: for `inherit` this fires on every
            # wave of every GAP repo that ships `tools/*.yaml`, and it is the
            # correct outcome — the worker keeps its OWN copy of this server.
            logger.info("worker_mcp.skipped_in_process_server: %r", name)
            continue
        selected[name] = entry

    if not selected:
        return child_options

    # The worker's own repo may declare a server under the same key. The
    # inherited one wins: it carries the live dispatch-time credential, whereas
    # a repo-declared entry only has `${VAR}`-substituted headers.
    merged: dict[str, Any] = {
        **child_options,
        "mcp_servers": {**(child_options.get("mcp_servers") or {}), **selected},
    }

    # Only EXTEND an allowlist the worker already has. Creating one where none
    # existed would flip "every tool allowed" into "only these MCP tools
    # allowed" and strip the worker of Read/Write/Bash. Workers run
    # `bypassPermissions` with no allowlist by default, so the no-op is correct.
    #
    # ⚠️ Extending is done for EVERY engine, not just the Anthropic-shaped ones.
    # A worker's allowlist comes from its roster entry's `tools` — which the
    # ORCHESTRATOR'S MODEL authored — so leaving it alone let a model-written
    # list silently REVOKE a grant the wave's configuration made. Measured: a
    # roster entry with `tools: ["write","read"]` produced workers that reported
    # a missing tool, made ZERO bridge calls, and still came back `completed`
    # under a passing scorecard. The rule is that the roster may narrow the
    # built-ins it was given; it may not hand back tools it was granted.
    existing = child_options.get("allowed_tools")
    if isinstance(existing, list):
        allowed = list(existing)
        for name, entry in selected.items():
            grants = _grants_for(engine_name, name, entry)
            if not grants:
                # gitclaw only, and only for an entry with no `?tools=` binding:
                # its allowlist is an exact-match set with no server-level form,
                # so there is no name we could add that would work. Say so — the
                # alternative is a worker that has the server and cannot call
                # any of it.
                logger.warning(
                    "worker_mcp.allowlist_unextendable: server %r on engine %r — the engine "
                    "matches allowed_tools exactly and this entry carries no ?tools= binding, "
                    "so the worker's roster `tools` list will hide this server.",
                    name,
                    engine_name,
                )
                continue
            for grant in grants:
                if grant not in allowed:
                    allowed.append(grant)
        merged["allowed_tools"] = allowed

    return merged


__all__ = ["inherit_mcp_into", "resolve_worker_mcp"]
