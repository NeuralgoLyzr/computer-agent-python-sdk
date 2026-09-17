"""Orchestration contract — the shared, engine-agnostic shape of a "main agent
that fans out worker sessions". Python port of the TS ``protocol/src/orchestration.ts``.

An identity loader (GAP) produces an :class:`OrchestrationSpec` on its load
result; the harness reads it to dispatch workers as sibling sessions.

Lives here rather than in a specific loader so the generic harness can consume
it without depending on any particular identity format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ── policy aliases ─────────────────────────────────────────────────────────

WorkerMcpPolicy = Any
"""Which of the ORCHESTRATOR's MCP servers a worker session gets.

MCP servers reach an agent as resolved ``options["mcp_servers"]`` on its
top-level session (the control plane binds them; the SDK materialises the
in-process server). A worker is a sibling session created inside the harness,
so it sees none of that unless it is propagated — this policy says whether to.

``"inherit"``
    every server the orchestrator resolved (the default)
``"none"``
    no inheritance; the worker keeps only its own repo's servers
``tuple[str, ...]``
    only these server names, by their ``mcp_servers`` key

.. warning::
   An inherited entry carries the dispatch-time credential in its headers, so a
   worker inherits the CALLER's MCP identity, not one of its own.
"""

OrchestrationMode = Literal["supervised", "roster"]
"""How the orchestrator triggers the fan-out.

``supervised``
    The orchestrator LLM calls a ``dispatch_many`` tool mid-session; reports
    return to it.
``roster``
    The orchestrator composes a roster and ends; code fans out, then
    measures/gates.
"""

CollectMode = Literal["into-context", "gate"]
"""What happens with the workers' results.

``into-context``
    Reports go back into the orchestrator's context.
``gate``
    Results feed a post-hoc scorecard/gate.
"""

OrchestrationIsolation = Literal["worktree", "none"]
"""Per-worker isolation strategy inside the orchestrator's workspace."""

SessionMode = Literal["separate", "sharedId"]
"""How a worker's telemetry identity relates to the orchestrator's.

``separate``
    Own ``session_id``, parent-linked → a nested child session. Default.
``sharedId``
    Fold the worker's telemetry under the orchestrator's ``session_id``.
"""


# ── dataclasses ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WorkerScope:
    """Per-worker filesystem scope; drives the scope-guard hook."""

    write: tuple[str, ...] | None = None
    read: tuple[str, ...] | None = None

    def is_empty(self) -> bool:
        return not self.write and not self.read


@dataclass(frozen=True)
class WorkerCrewEntry:
    """One referenced worker in an orchestrator's crew.

    Declarative: ``use`` points at a DIFFERENT repo / registered agent id; the
    harness resolves it and attaches the per-worker task at dispatch time.
    """

    #: Stable id within the crew (the ``agents/<name>`` basename).
    name: str
    #: The referenced GAP source: a git URL (optionally ``@ref``) or a
    #: registered agent id.
    use: str
    #: ``True`` ⇒ own session_id (nested child); ``False`` ⇒ shared telemetry id.
    separate_session: bool = True
    scope: WorkerScope | None = None
    budget_usd: float | None = None
    max_turns: int | None = None
    model: str | None = None
    tool_subset: tuple[str, ...] | None = None
    #: Per-worker override of :attr:`OrchestrationSpec.worker_mcp`. ``None`` ⇒
    #: use the spec's.
    mcp: WorkerMcpPolicy | None = None


@dataclass(frozen=True)
class DispatchRequestItem:
    """One worker an orchestrator asks to spawn via the in-session
    ``dispatch_many`` tool (Surface A, supervised mode).

    Mirrors a roster entry but supplied directly by the orchestrator LLM at
    tool-call time.
    """

    task: str
    id: str | None = None
    use: str | None = None
    role: str | None = None
    scope: WorkerScope | None = None
    model: str | None = None
    tools: tuple[str, ...] | None = None


@dataclass(frozen=True)
class DispatchResultItem:
    """The report of one dispatched worker, returned into the orchestrator's
    context."""

    id: str
    status: str
    text: str
    error: str | None = None


@dataclass(frozen=True)
class OrchestrationSpec:
    """The orchestration configuration + resolved crew for one agent."""

    mode: OrchestrationMode = "roster"
    #: Max workers in flight at once.
    max_parallel: int = 4
    collect: CollectMode = "into-context"
    isolation: OrchestrationIsolation = "worktree"
    #: Stage budget (USD) shared across the wave; falls back to
    #: ``runtime.budget_usd``.
    budget_usd: float | None = None
    #: Default MCP inheritance for every worker in the wave; a crew entry's own
    #: ``mcp`` overrides it. The GAP loader always sets this, but it stays
    #: optional so a hand-built spec need not. Resolve it through
    #: :func:`computeragent.orchestration.worker_mcp.resolve_worker_mcp` rather
    #: than reading it directly — absent means ``"inherit"``, not ``"none"``.
    worker_mcp: WorkerMcpPolicy | None = None
    #: The referenced worker crew (empty when only inline sub-agents exist).
    workers: tuple[WorkerCrewEntry, ...] = field(default_factory=tuple)


# ── the harness's own control-plane tools ──────────────────────────────────

ORCHESTRATION_SUPERVISED_DISPATCH_MANY_TOOL_NAME = "mcp__orchestration__dispatch_many"
"""The tool the harness injects into a SUPERVISED orchestrator so it can ask for
a fan-out (the in-process ``orchestration`` MCP server).

Lives here, not in the engine, because the harness's policy gate has to
recognise the name.

The identifier spells out orchestration + mode + tool deliberately: as more
orchestration-injected tools land, a bare ``DISPATCH_TOOL_NAME`` next to them
stops saying which mode injects it or what it dispatches. Name new siblings the
same way — ``ORCHESTRATION_<MODE>_<TOOL>_TOOL_NAME``.
"""

ORCHESTRATION_INTERNAL_TOOLS: tuple[str, ...] = (ORCHESTRATION_SUPERVISED_DISPATCH_MANY_TOOL_NAME,)
"""Tools the HARNESS ITSELF injects to drive orchestration — control plane, not
capability the agent was granted. Every entry is a tool that (a) exists only
because the harness put it there, and (b) performs no action on the outside
world; it asks the harness to do orchestration bookkeeping.

Names are the fully-namespaced form the MODEL sees, because that is the string
the policy gate is handed. Note the two engines namespace differently —
``claude-agent-sdk`` prefixes ``mcp__<server>__``, gitclaw uses ``<server>__`` —
so a future tool reachable from both engines needs BOTH spellings listed here.
Get the spelling wrong and the effect is silent: the tool is simply treated as
ordinary agent capability again.

── Why this list exists ────────────────────────────────────────────────────
A tool on this list is NOT put to the policy decider at all. The gate
short-circuits it to *allow* before evaluating, because there is nothing here
for a policy to protect:

* the call itself touches no file, host, or network;
* everything the workers it starts then DO arrives back at the same tool
  boundary, where the same policy still applies in full.

So the only thing a verdict on these tools can change is whether orchestration
works — and both possible verdicts are bad:

1. DENY BY OMISSION. Cedar is default-deny. An operator writing a policy for
   their agent's real tools has no reason to know a harness internal called
   ``mcp__orchestration__dispatch_many`` exists, so nothing permits it and every
   supervised fan-out is denied by a policy behaving exactly as written. This is
   a verdict, not an outage, so no fail-open on a no-verdict result can rescue
   it.
2. NO VERDICT. A deployment whose SRS credential wasn't registered 403'd on the
   policy fetch, which failed ``dispatch_many`` closed and killed the
   orchestrator's turn — while the OTHER SRS consumer, the redaction proxy, had
   already chosen to fail open for exactly this reason. Roster mode kept working
   throughout, because it never makes a tool call at all.

Bypassing also stops our control plane leaking into the customer's policy:
otherwise adding an internal tool silently breaks every existing policy until
each one is updated to permit it.

── What this does NOT do ───────────────────────────────────────────────────
It does not weaken the gate for anything the agent actually does. Real
capability — including every tool call made by every worker the fan-out starts
— still fails CLOSED on a no-verdict result. The bypass is logged
(``policy.control_plane_bypass``) and still emits a decision event so the trace
shows the fan-out.

── Adding a future orchestration tool ──────────────────────────────────────
Append its model-facing name here and it inherits the same treatment. Before you
do, check it against the two conditions above — a tool that only reports or
requests orchestration state qualifies; one that acquires new capability for the
agent (writes a file, spends money, calls a third party, widens a worker's
scope) does NOT, and must stay off this list even though the harness is what
injected it. When in doubt, leave it off: the cost of omission is a fan-out an
operator must explicitly permit, and the cost of a wrong inclusion is a tool
that bypasses the guardrail entirely.
"""


def is_orchestration_internal_tool(tool_name: str) -> bool:
    """Is this tool call the harness's own orchestration control plane rather
    than agent capability?

    When true the policy gate allows it WITHOUT consulting the decider — see
    :data:`ORCHESTRATION_INTERNAL_TOOLS` for the reasoning and for how to extend
    the set.
    """
    return tool_name in ORCHESTRATION_INTERNAL_TOOLS


def normalize_mcp_policy(value: Any) -> WorkerMcpPolicy | None:
    """Coerce a parsed ``mcp:`` / ``worker_mcp:`` value to the policy shape.

    Anything unrecognised becomes ``None`` (⇒ "nothing said here"), which
    ``resolve_worker_mcp`` then resolves to the next level's default. A list of
    server names keeps only the strings, and an empty list is a deliberate
    "these zero servers" rather than a fallthrough — it is kept as an empty
    tuple so it still reads as "no inheritance" at the resolve site.
    """
    if value in ("inherit", "none"):
        return value
    if isinstance(value, (list, tuple)):
        return tuple(v for v in value if isinstance(v, str) and v)
    return None


__all__ = [
    "ORCHESTRATION_INTERNAL_TOOLS",
    "ORCHESTRATION_SUPERVISED_DISPATCH_MANY_TOOL_NAME",
    "CollectMode",
    "DispatchRequestItem",
    "DispatchResultItem",
    "OrchestrationIsolation",
    "OrchestrationMode",
    "OrchestrationSpec",
    "SessionMode",
    "WorkerCrewEntry",
    "WorkerMcpPolicy",
    "WorkerScope",
    "is_orchestration_internal_tool",
    "normalize_mcp_policy",
]
