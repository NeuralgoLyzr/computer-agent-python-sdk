"""Shared-codebase mode. The orchestrator's workspace IS the feature repo; each
inline roster worker gets its own git worktree off a shared baseline, edits its
scoped slice, and the branches are MERGED — a merge conflict is direct evidence
of a scope overlap.

Workers are defined INLINE by the roster (``role``, ``scope``, ``model``,
``tools``); a minimal synthetic identity is supplied per worker (and excluded
from diff attribution). Never raises.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from .budget import BudgetAllocator, BudgetExceeded, warn_unenforceable_budget
from .engine_shape import is_gitclaw_shaped
from .parse_roster import parse_roster_result
from .spawn_child import (
    WorkerResult,
    WorkerSpec,
    WorkerUsage,
    _scope_payload,
    _usage_payload,
    collect_session,
    inline_worker_manifest,
)
from .worker_mcp import inherit_mcp_into, resolve_worker_mcp
from .worktree import (
    WorkerTree,
    cleanup,
    commit_worker,
    create_worktree,
    diff_paths,
    init_baseline,
    integrate,
    restore_baseline_paths,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .budget import BudgetSummary
    from .contract import OrchestrationSpec
    from .parse_roster import RosterEntry, RosterOutcome
    from .spawn_child import OrchestratorRef
    from .wave_events import WaveReporter

logger = logging.getLogger("computeragent.orchestration.worktree_dispatch")

#: Paths an inline worker's identity would materialise into its worktree, which
#: must be restored to their baseline afterwards. This SDK's inline loader
#: writes nothing (see ``_InlineManifestLoader``), so the restore is a
#: belt-and-braces measure that keeps the invariant if that ever changes.
INLINE_IDENTITY_PATHS = ("agent.yaml",)


@dataclass(frozen=True)
class WorktreeRunResult:
    orchestrator_text: str = ""
    dispatched: int = 0
    workers: tuple[WorkerResult, ...] = field(default_factory=tuple)
    #: Paths present in the merged tree, relative to the baseline.
    integrated: tuple[str, ...] = field(default_factory=tuple)
    #: worker_id → conflicting paths (a non-empty entry = a scope overlap that bit).
    conflicts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Budget accounting for the wave, when a stage budget was set.
    budget: BudgetSummary | None = None
    #: Set when the wave was refused before dispatch for being under-funded.
    aborted: str | None = None
    outcome: RosterOutcome | None = None


async def dispatch_worktree_roster(
    parent: OrchestratorRef,
    spec: OrchestrationSpec,
    text: str,
    reporter: WaveReporter | None = None,
) -> WorktreeRunResult:
    parsed = parse_roster_result(text)
    # An empty roster PLANNED nothing, so there is no wave to report —
    # announcing a started/ended pair for it is what rendered a "sub-agents
    # dispatched and merged" panel over a run that dispatched nobody.
    # Withholding the reporter suppresses exactly that pair and nothing else:
    # `entries` is empty by definition when the outcome is `empty-roster`, so no
    # real worker event can be lost this way. Every OTHER zero-worker outcome
    # keeps its events — those are genuine roster failures and the loudness is
    # the point.
    wave_reporter = None if parsed.outcome == "empty-roster" else reporter
    result = await dispatch_worktree_entries(
        parent, spec, parsed.entries, orchestrator_text=text, reporter=wave_reporter
    )
    return replace(result, outcome=parsed.outcome)


async def dispatch_worktree_entries(
    parent: OrchestratorRef,
    spec: OrchestrationSpec,
    entries: Sequence[RosterEntry],
    *,
    orchestrator_text: str = "",
    reporter: WaveReporter | None = None,
) -> WorktreeRunResult:
    """The entries-based core — shared by the roster runner (which parses the
    orchestrator's text) and the supervised ``dispatch_many`` tool (which
    supplies the workers directly)."""

    # Worker ids are positional here (`entry.id or w<i+1>`), so they can be
    # announced before anything runs — but they MUST be derived exactly the way
    # the pool below derives them, or the caller's rows never match their events.
    def worker_id(i: int) -> str:
        entry_id = entries[i].id if i < len(entries) else None
        return entry_id or f"w{i + 1}"

    def announce() -> None:
        if reporter is None:
            return
        reporter.wave_started(
            mode="supervised" if spec.mode == "supervised" else "roster",
            isolation="worktree",
            max_parallel=max(1, spec.max_parallel),
            planned=[
                {
                    "id": worker_id(i),
                    **({"role": e.role} if e.role else {}),
                    **({"task": e.task} if e.task else {}),
                }
                for i, e in enumerate(entries)
            ],
        )

    if not entries:
        announce()
        if reporter is not None:
            reporter.wave_ended(dispatched=0, integrated=[], conflicts={})
        return WorktreeRunResult(orchestrator_text=orchestrator_text)

    # Fair-share budget: compute each worker's cap ONCE against the wave width;
    # an under-funded wave is refused before any spend (a clean, reportable
    # abort).
    budget: BudgetAllocator | None = None
    worker_cap: float | None = None
    summary: BudgetSummary | None = None
    if spec.budget_usd is not None:
        budget = BudgetAllocator(spec.budget_usd)
        try:
            worker_cap = budget.check_before_dispatch(len(entries))
        except BudgetExceeded as exc:
            announce()
            summary = budget.summary()
            if reporter is not None:
                reporter.wave_ended(
                    dispatched=0,
                    integrated=[],
                    conflicts={},
                    aborted=str(exc),
                    budget=summary,
                )
            return WorktreeRunResult(
                orchestrator_text=orchestrator_text, budget=summary, aborted=str(exc)
            )
        # The cap rides on every worker as `max_budget_usd`, but an engine that
        # declares `capabilities.budget is False` ignores it — say so rather
        # than reporting a fair-share cap that nothing enforces.
        warn_unenforceable_budget(
            getattr(parent.engine, "capabilities", None),
            engine=parent.engine_name,
            cap_usd=worker_cap,
            workers=len(entries),
        )

    announce()
    base_sha = await init_baseline(parent.workdir)
    limit = max(1, min(spec.max_parallel, len(entries)))
    results: list[WorkerResult | None] = [None] * len(entries)
    trees: list[WorkerTree | None] = [None] * len(entries)
    cursor = 0
    cursor_lock = asyncio.Lock()

    async def run_one() -> None:
        nonlocal cursor
        while True:
            async with cursor_lock:
                index = cursor
                cursor += 1
            if index >= len(entries):
                return
            entry = entries[index]
            wid = worker_id(index)
            child_sid = f"sess_{wid}"
            started_at = time.monotonic()
            try:
                tree = await create_worktree(parent.workdir, wid, base_sha)
                trees[index] = tree

                worker_spec = WorkerSpec(
                    id=wid,
                    source=tree.path,
                    task=entry.task,
                    scope=entry.scope,
                    inline_manifest=inline_worker_manifest(wid),
                    # Inline workers have a synthetic identity, so their loader
                    # contributes no MCP servers at all — inheriting the
                    # orchestrator's is their ONLY route to the org's tools. A
                    # roster entry cannot override this: the roster is
                    # LLM-authored, and letting the model name its own tool
                    # grants would be self-escalation.
                    options=inherit_mcp_into(
                        _worker_options(
                            wid,
                            entry,
                            parent.engine_name,
                            budget_cap=worker_cap,
                            parent_options=parent.engine_options,
                        ),
                        parent.engine_options,
                        resolve_worker_mcp(None, spec.worker_mcp),
                        parent.engine_name,
                    ),
                )

                from .spawn_child import _build_child_coordinator

                coordinator, engine_name = _build_child_coordinator(
                    parent,
                    worker_spec,
                    child_session_id=child_sid,
                    options=dict(worker_spec.options or {}),
                    workdir=tree.path,
                )
                if reporter is not None:
                    reporter.worker_started(
                        worker_id=wid,
                        worker_session_id=child_sid,
                        role=entry.role,
                        task=entry.task or None,
                        engine=engine_name,
                        model=entry.model,
                        scope=_scope_payload(entry.scope),
                    )
                # Belt and braces: restore anything an identity path wrote into
                # this worktree, so the worker's diff carries only the worker's
                # own work and multi-worker waves cannot collide on a file none
                # of them touched.
                await restore_baseline_paths(tree, INLINE_IDENTITY_PATHS)

                try:
                    outcome = await collect_session(coordinator, entry.task)
                finally:
                    try:
                        await coordinator.dispose()
                    except Exception:  # noqa: BLE001 — teardown must not mask the result
                        logger.debug("worker %s: dispose failed", wid, exc_info=True)

                artifacts = await diff_paths(tree)
                await commit_worker(tree, f"{wid}: {entry.role or 'worker'}")
                if budget is not None:
                    budget.record(
                        wid,
                        outcome.usage.cost_usd or 0.0,
                        "sdk" if outcome.usage.cost_usd is not None else "estimated",
                    )
                results[index] = WorkerResult(
                    id=wid,
                    session_id=outcome.session_id,
                    status=outcome.status,
                    text=outcome.text,
                    usage=outcome.usage,
                    error=outcome.error,
                    artifacts=artifacts,
                )
                # `artifacts` is available per worker HERE (git diff against the
                # wave's baseline, before the merge), so the completion carries
                # the files this worker actually wrote — not a wave-level total
                # the caller would have to attribute itself.
                if reporter is not None:
                    reporter.worker_completed(
                        worker_id=wid,
                        worker_session_id=outcome.session_id,
                        status=outcome.status,
                        error=outcome.error,
                        text=outcome.text or None,
                        artifacts=list(artifacts),
                        usage=_usage_payload(outcome.usage),
                        duration_ms=(time.monotonic() - started_at) * 1000.0,
                    )
            except Exception as exc:  # noqa: BLE001 — fail-soft; never lose the wave
                message = str(exc) or type(exc).__name__
                results[index] = WorkerResult(
                    id=wid,
                    session_id="",
                    status="errored",
                    text="",
                    usage=WorkerUsage(),
                    error=message,
                )
                # A worker that died before its session existed (its worktree
                # could not be created, say) has no session id to link to —
                # report the failure anyway rather than leaving its row spinning
                # forever.
                if reporter is not None:
                    reporter.worker_completed(
                        worker_id=wid,
                        worker_session_id="",
                        status="errored",
                        error=message,
                        usage=_usage_payload(WorkerUsage()),
                        duration_ms=(time.monotonic() - started_at) * 1000.0,
                    )

    await asyncio.gather(*(run_one() for _ in range(limit)))

    committed = [t for t in trees if t is not None]
    merged = await integrate(parent.workdir, committed, base_sha)
    await cleanup(parent.workdir, committed)

    summary = budget.summary() if budget is not None else None
    # After the merge, so `integrated` / `conflicts` are final. A conflict entry
    # is the scope-overlap finding itself, which is why it rides on the wave
    # rather than on either worker's completion.
    if reporter is not None:
        reporter.wave_ended(
            dispatched=len(entries),
            integrated=list(merged.integrated),
            conflicts={k: list(v) for k, v in merged.conflicts.items()},
            budget=summary,
        )

    return WorktreeRunResult(
        orchestrator_text=orchestrator_text,
        dispatched=len(entries),
        workers=tuple(r for r in results if r is not None),
        integrated=merged.integrated,
        conflicts=merged.conflicts,
        budget=summary,
    )


def _parent_model_of(parent_options: Any) -> str | None:
    """The orchestrator's own resolved ``model``, when it has one."""
    if not isinstance(parent_options, dict):
        return None
    model = parent_options.get("model")
    return model if isinstance(model, str) and model else None


def _worker_options(
    worker_id: str,
    entry: RosterEntry,
    engine_name: str,
    *,
    budget_cap: float | None = None,
    parent_options: Any = None,
) -> dict[str, Any]:
    """Engine options for an inline worker — role + scope in the prompt."""
    scope_line = (
        f" Write ONLY within: {', '.join(entry.scope.write)}."
        if entry.scope is not None and entry.scope.write
        else ""
    )
    append = (
        f'You are worker "{worker_id}"'
        + (f" (role: {entry.role})" if entry.role else "")
        + "."
        + scope_line
        + " Implement your task inside this repository, then stop."
    )
    # Model: the roster entry's choice, else INHERIT the orchestrator's resolved
    # model. Without the fallback a worker sent no model at all and the engine's
    # CLI substituted its own built-in default — which a gateway-routed
    # deployment rejects outright. Under a native Lyzr Studio gateway the model
    # must be that org's Studio agent id, a value the orchestrator LLM cannot
    # know, so every worker died on an opaque upstream 500 while the
    # orchestrator itself ran fine. `envs` and MCP servers are already inherited
    # at the spawn site; the model belongs with them.
    model = entry.model or _parent_model_of(parent_options)

    out: dict[str, Any] = {}
    # The worker's identity has to reach the model through whichever field its
    # engine APPENDS with. The inline worker has no loader-built prompt, so for
    # a gitclaw-shaped engine the append text IS its `system_prompt`; handing it
    # the Claude preset object would stringify into nonsense instead.
    if is_gitclaw_shaped(engine_name):
        out["system_prompt"] = append
    else:
        out["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": append,
        }
    out["permission_mode"] = "bypassPermissions"
    if entry.tools:
        out["allowed_tools"] = list(entry.tools)
    if model:
        out["model"] = model
    # The fair-share per-worker cap, enforced in-session by engines that declare
    # `capabilities.budget` — the dispatch site warns when the wave's engine
    # does not, so the cap is never reported as binding when it isn't.
    if budget_cap is not None:
        out["max_budget_usd"] = budget_cap
    return out


__all__ = [
    "INLINE_IDENTITY_PATHS",
    "WorktreeRunResult",
    "dispatch_worktree_entries",
    "dispatch_worktree_roster",
]
