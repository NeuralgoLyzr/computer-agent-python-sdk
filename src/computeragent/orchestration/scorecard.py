"""Measurement + the gate for a shared-codebase run.

The gate verdict is computed from what the run already produced: every worker
completed, no merge conflict (a conflict IS a scope-overlap finding), and no
budget abort. This is the mechanical, un-gameable core; the heavier measures
(held-out test suite, self-report fidelity, orchestrator behaviour) are a later
layer that needs worker reports + a test runner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .budget import BudgetSummary

GateVerdict = Literal["pass", "fail"]


@dataclass(frozen=True)
class GateResult:
    verdict: GateVerdict
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ScorecardWorker:
    id: str
    status: str
    artifacts: int
    cost_usd: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class Scorecard:
    agent: str
    status: GateVerdict
    workers: tuple[ScorecardWorker, ...]
    integrated: tuple[str, ...]
    conflicts: Mapping[str, tuple[str, ...]]
    gate: GateResult
    budget: BudgetSummary | None = None


def evaluate_gate(
    *,
    workers: Sequence[Any],
    conflicts: Mapping[str, Sequence[str]],
    aborted: str | None = None,
) -> GateResult:
    reasons: list[str] = []

    if aborted:
        reasons.append(f"budget aborted the wave: {aborted}")

    # A wave that dispatched NOBODY is a failure, not a pass. Without this the
    # gate certified every silent roster failure as green: with no workers there
    # is no worker to complain about, so `reasons` stayed empty and the verdict
    # came back "pass" for a run that did nothing at all.
    if len(workers) == 0 and not aborted:
        reasons.append("no workers were dispatched — the roster was empty or did not parse")

    for w in workers:
        if w.status != "completed" or w.error:
            reasons.append(f"worker {w.id} did not complete cleanly: {w.error or w.status}")

    for worker_id, paths in conflicts.items():
        reasons.append(f"worker {worker_id} conflicts on {', '.join(paths)} (scope overlap)")

    return GateResult(verdict="pass" if not reasons else "fail", reasons=tuple(reasons))


def build_scorecard(
    agent: str,
    *,
    workers: Sequence[Any],
    conflicts: Mapping[str, Sequence[str]],
    integrated: Sequence[str],
    aborted: str | None = None,
    budget: BudgetSummary | None = None,
) -> Scorecard:
    gate = evaluate_gate(workers=workers, conflicts=conflicts, aborted=aborted)
    return Scorecard(
        agent=agent,
        status=gate.verdict,
        workers=tuple(
            ScorecardWorker(
                id=w.id,
                status=w.status,
                artifacts=len(w.artifacts or ()),
                cost_usd=w.usage.cost_usd,
                error=w.error,
            )
            for w in workers
        ),
        integrated=tuple(integrated),
        conflicts={k: tuple(v) for k, v in conflicts.items()},
        gate=gate,
        budget=budget,
    )


__all__ = [
    "GateResult",
    "GateVerdict",
    "Scorecard",
    "ScorecardWorker",
    "build_scorecard",
    "evaluate_gate",
]
