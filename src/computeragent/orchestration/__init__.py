"""Sub-agent orchestration — an agent that fans out worker sessions.

Two surfaces, chosen by the agent's ``orchestration.mode``:

``roster``
    The orchestrator composes a JSON roster and ends its turn; the harness parses
    it, fans out, and feeds the workers' reports back for one closing turn.
``supervised``
    The orchestrator gets an in-session ``dispatch_many`` tool and asks for
    workers mid-turn; the reports return as the tool result.

Two isolation strategies, chosen by ``orchestration.isolation``:

``worktree``
    Shared-codebase. Workers are defined inline by the roster and each edits the
    orchestrator's own repo in its own git worktree; branches are merged, and a
    conflict is direct evidence of a scope overlap.
``none``
    Independent. Each worker clones the referenced repo named by its crew entry
    (``agents/<name>.yaml`` with ``use:``) and runs as its own session.

Ported from the TypeScript harness-server's orchestration services; the
divergences from that port are documented at each site that makes one.
"""

from .budget import BudgetAllocator, BudgetExceeded, BudgetSummary
from .composite_decider import CompositePolicyDecider, compose_deciders
from .contract import (
    ORCHESTRATION_INTERNAL_TOOLS,
    ORCHESTRATION_SUPERVISED_DISPATCH_MANY_TOOL_NAME,
    CollectMode,
    DispatchRequestItem,
    DispatchResultItem,
    OrchestrationIsolation,
    OrchestrationMode,
    OrchestrationSpec,
    SessionMode,
    WorkerCrewEntry,
    WorkerMcpPolicy,
    WorkerScope,
    is_orchestration_internal_tool,
    normalize_mcp_policy,
)
from .crew import crew_entry_to_worker_spec, use_to_source
from .parse_roster import (
    RosterEntry,
    RosterOutcome,
    RosterParse,
    is_roster_text,
    parse_roster,
    parse_roster_result,
)
from .roster_contract import ROSTER_CONTRACT, append_roster_contract
from .roster_run import (
    RosterRunResult,
    direct_answer_reason,
    dispatch_roster,
    dispatch_roster_entries,
    gate_applies,
    no_reports_to_fold,
    synthesis_enabled,
    wave_needs_synthesis,
)
from .scope_guard import AllowAllDecider, ScopeGuardDecider
from .scorecard import GateResult, Scorecard, build_scorecard, evaluate_gate
from .spawn_child import (
    OrchestratorRef,
    WorkerResult,
    WorkerSpec,
    WorkerUsage,
    dispatch_workers,
    spawn_child_session,
)
from .wave_events import WAVE_EVENT_KINDS, WaveReporter
from .worker_digest import build_direct_answer_prompt, build_synthesis_prompt
from .worker_mcp import inherit_mcp_into, resolve_worker_mcp
from .worktree_dispatch import (
    WorktreeRunResult,
    dispatch_worktree_entries,
    dispatch_worktree_roster,
)

__all__ = [
    "ORCHESTRATION_INTERNAL_TOOLS",
    "ORCHESTRATION_SUPERVISED_DISPATCH_MANY_TOOL_NAME",
    "ROSTER_CONTRACT",
    "WAVE_EVENT_KINDS",
    "AllowAllDecider",
    "BudgetAllocator",
    "BudgetExceeded",
    "BudgetSummary",
    "CollectMode",
    "CompositePolicyDecider",
    "DispatchRequestItem",
    "DispatchResultItem",
    "GateResult",
    "OrchestrationIsolation",
    "OrchestrationMode",
    "OrchestrationSpec",
    "OrchestratorRef",
    "RosterEntry",
    "RosterOutcome",
    "RosterParse",
    "RosterRunResult",
    "Scorecard",
    "ScopeGuardDecider",
    "SessionMode",
    "WaveReporter",
    "WorkerCrewEntry",
    "WorkerMcpPolicy",
    "WorkerResult",
    "WorkerScope",
    "WorkerSpec",
    "WorkerUsage",
    "WorktreeRunResult",
    "append_roster_contract",
    "build_direct_answer_prompt",
    "build_scorecard",
    "build_synthesis_prompt",
    "compose_deciders",
    "crew_entry_to_worker_spec",
    "direct_answer_reason",
    "dispatch_roster",
    "dispatch_roster_entries",
    "dispatch_workers",
    "dispatch_worktree_entries",
    "dispatch_worktree_roster",
    "evaluate_gate",
    "gate_applies",
    "inherit_mcp_into",
    "is_orchestration_internal_tool",
    "is_roster_text",
    "no_reports_to_fold",
    "normalize_mcp_policy",
    "parse_roster",
    "parse_roster_result",
    "resolve_worker_mcp",
    "spawn_child_session",
    "synthesis_enabled",
    "use_to_source",
    "wave_needs_synthesis",
]
