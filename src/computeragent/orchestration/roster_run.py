"""Independent (clone) mode, plus the decisions that close out a roster run.

Roster mode ends the orchestrator's turn with its ROSTER as the final text, so
without a closing turn the caller gets wave events and no answer. Feeding the
workers' reports back for one more turn fixes that (and is what
``collect: "into-context"`` always claimed to do).

Kill switch for an operator who wants the old behaviour — or to save the one
extra LLM call: ``ORCHESTRATOR_SYNTHESIS=0``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from .budget import BudgetAllocator, BudgetExceeded, warn_unenforceable_budget
from .crew import crew_entry_to_worker_spec
from .parse_roster import is_roster_text, parse_roster_result
from .spawn_child import WorkerResult, WorkerSpec, dispatch_workers
from .worker_mcp import resolve_worker_mcp

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .budget import BudgetSummary
    from .contract import OrchestrationSpec, WorkerCrewEntry
    from .parse_roster import RosterEntry, RosterOutcome
    from .spawn_child import OrchestratorRef
    from .wave_events import WaveReporter

logger = logging.getLogger("computeragent.orchestration.roster")


def synthesis_enabled() -> bool:
    return os.environ.get("ORCHESTRATOR_SYNTHESIS") not in ("0", "false")


@dataclass(frozen=True)
class RosterRunResult:
    #: The orchestrator's composed-roster output text.
    orchestrator_text: str = ""
    #: How many workers were dispatched (matched a crew member).
    dispatched: int = 0
    workers: tuple[WorkerResult, ...] = field(default_factory=tuple)
    #: Roster entries whose ``use``/``id`` matched no crew member (skipped).
    unmatched: tuple[str, ...] = field(default_factory=tuple)
    #: Budget accounting for the wave, when a stage budget was set.
    budget: BudgetSummary | None = None
    #: Set when the wave was refused before dispatch for being under-funded.
    aborted: str | None = None
    outcome: RosterOutcome | None = None


# ── the three decisions a closing turn turns on ────────────────────────────


def wave_needs_synthesis(result: Any, outcome: RosterOutcome) -> bool:
    """Did this wave produce something only a closing turn can tell the user
    about?

    A ``mode: "roster"`` agent is NOT obliged to fan out — the task was small
    enough, or it decided to do the work itself — and then its own turn text is
    already the final answer. Synthesising there is wrong twice over: it spends
    an extra LLM call, and its result REPLACES that answer with a rewrite
    prompted by "Wave summary: no workers ran" — so a completed single-agent
    task comes back as a summary of nothing, or a flat refusal to summarise.

    So the question for a zero-worker wave is only ever: IS THE ORCHESTRATOR'S
    OWN TEXT SHOWABLE? :func:`is_roster_text` answers it. Roster text is machine
    input — the harness parsed it, the user must never see it — so those runs
    take the closing turn no matter how little the wave did. Prose is already
    the answer and is left exactly as written.

    ``aborted`` (the budget refused the wave) and ``unmatched`` (the roster
    named workers in no crew) still force the turn on their own: both imply
    roster text, and both are config errors that must not be swallowed.
    """
    if (
        len(result.workers) > 0
        or result.aborted is not None
        or len(getattr(result, "unmatched", ()) or ()) > 0
    ):
        return True
    return is_roster_text(outcome)


def gate_applies(outcome: RosterOutcome) -> bool:
    """Should a ``collect: "gate"`` run be scored at all?

    The gate answers "did the fan-out this run ATTEMPTED come out clean?".
    Exactly ONE outcome attempted none on purpose — ``empty-roster``, where the
    agent emitted the roster shape with nobody in it because the task needed no
    delegation. Scoring that produced the worst possible verdict (``fail``,
    reason "no workers were dispatched") for a turn where nothing was wrong.

    Everything else stays scored, including the prose outcomes: under a roster
    agent, answering without emitting a roster at all is a deviation from the
    contract its prompt states, and the gate is where an operator finds that
    out. ``invalid-json`` / ``no-usable-entries`` / unmatched entries are the
    silent roster failures the zero-worker check was added for — they must keep
    failing loudly.
    """
    return outcome != "empty-roster"


def no_reports_to_fold(result: Any) -> bool:
    """Is there genuinely nothing for a closing turn to report on — no worker
    reports, no abort, no unmatched entries? Then the turn exists solely because
    the orchestrator's own text was roster JSON, and it needs the direct-answer
    prompt rather than a wave digest."""
    return (
        len(result.workers) == 0
        and result.aborted is None
        and len(getattr(result, "unmatched", ()) or ()) == 0
    )


def direct_answer_reason(
    outcome: RosterOutcome,
) -> Literal["empty-roster", "unparsed-roster"]:
    """Which flavour of "your last message was a roster" to tell the model."""
    return "empty-roster" if outcome == "empty-roster" else "unparsed-roster"


# ── dispatch ───────────────────────────────────────────────────────────────


def match_crew(workers: Sequence[WorkerCrewEntry], entry: RosterEntry) -> WorkerCrewEntry | None:
    """Match a roster entry to a crew member.

    Precedence: ``use`` names the crew template → else ``id`` names it → else
    ``use`` matches a crew ``use:`` → else a single-worker crew is unambiguous
    even when the roster omits ``use``.
    """
    for candidate in workers:
        if candidate.name == entry.use:
            return candidate
    for candidate in workers:
        if candidate.name == entry.id:
            return candidate
    for candidate in workers:
        if candidate.use == entry.use:
            return candidate
    return workers[0] if len(workers) == 1 else None


async def dispatch_roster(
    parent: OrchestratorRef,
    spec: OrchestrationSpec,
    text: str,
    reporter: WaveReporter | None = None,
) -> RosterRunResult:
    """Fan out the workers named by a composed-roster ``text``.

    Pure dispatch: it does NOT run the orchestrator — the caller supplies the
    orchestrator's already-collected roster text. Never raises.
    """
    parsed = parse_roster_result(text)
    # See the same guard in the worktree dispatcher: an empty roster planned
    # nothing, so it gets no wave events. Other zero-worker outcomes keep theirs.
    wave_reporter = None if parsed.outcome == "empty-roster" else reporter
    result = await dispatch_roster_entries(
        parent, spec, parsed.entries, orchestrator_text=text, reporter=wave_reporter
    )
    return replace(result, outcome=parsed.outcome)


async def dispatch_roster_entries(
    parent: OrchestratorRef,
    spec: OrchestrationSpec,
    entries: Sequence[RosterEntry],
    *,
    orchestrator_text: str = "",
    reporter: WaveReporter | None = None,
) -> RosterRunResult:
    """Entries-based core — shared by the roster runner and the supervised
    ``dispatch_many`` tool."""
    specs: list[WorkerSpec] = []
    unmatched: list[str] = []
    for entry in entries:
        crew = match_crew(spec.workers, entry)
        if crew is None:
            unmatched.append(entry.use or entry.id or "?")
            continue
        # Crew supplies the config; the roster supplies the per-instance id +
        # task. MCP precedence is resolved here because this is the only place
        # that sees BOTH the crew entry and the orchestration block.
        built = crew_entry_to_worker_spec(crew, entry.task)
        specs.append(
            replace(
                built,
                id=entry.id or crew.name,
                mcp=resolve_worker_mcp(crew.mcp, spec.worker_mcp),
            )
        )

    # `wave_started` is emitted only once the crew match has run, because THAT
    # is when a worker's id becomes authoritative (`entry.id or crew.name`).
    # Emitting from the parsed roster would announce ids that dispatch then
    # renames, and a caller keying its rows on them would never match a single
    # later event.
    def announce() -> None:
        if reporter is None:
            return
        reporter.wave_started(
            mode="supervised" if spec.mode == "supervised" else "roster",
            isolation="none",
            max_parallel=max(1, spec.max_parallel),
            planned=[
                {
                    "id": w.id,
                    **({"use": w.source} if isinstance(w.source, str) else {}),
                    **({"task": w.task} if w.task else {}),
                }
                for w in specs
            ],
        )

    if not specs:
        # A roster that named workers and matched NONE of them still gets its
        # pair of wave events — that is a real config error and the gate fails
        # on it. The one zero-worker case that must stay silent (an empty
        # roster: the agent chose not to fan out) never arrives here with a
        # reporter — `dispatch_roster` withholds it — so this no longer has to
        # guess which case it is in.
        announce()
        if reporter is not None:
            reporter.wave_ended(dispatched=0, unmatched=list(unmatched) or None)
        return RosterRunResult(orchestrator_text=orchestrator_text, unmatched=tuple(unmatched))

    # Fair-share budget: cap each worker against the wave width; refuse an
    # under-funded wave before dispatch (the per-worker cap is min'd with any
    # the crew already declared, so a tighter crew cap still wins).
    budget: BudgetAllocator | None = None
    dispatch_specs = specs
    summary: BudgetSummary | None = None
    if spec.budget_usd is not None:
        budget = BudgetAllocator(spec.budget_usd)
        try:
            cap = budget.check_before_dispatch(len(specs))
        except BudgetExceeded as exc:
            announce()
            summary = budget.summary()
            if reporter is not None:
                reporter.wave_ended(
                    dispatched=0,
                    unmatched=list(unmatched) or None,
                    aborted=str(exc),
                    budget=summary,
                )
            return RosterRunResult(
                orchestrator_text=orchestrator_text,
                unmatched=tuple(unmatched),
                budget=summary,
                aborted=str(exc),
            )
        dispatch_specs = [
            replace(
                s,
                options={
                    **(s.options or {}),
                    "max_budget_usd": min(
                        (s.options or {}).get("max_budget_usd", float("inf")), cap
                    ),
                },
            )
            for s in specs
        ]
        # A referenced worker may name its own engine, so count the wave per
        # engine and warn for each one that cannot honour the cap it was handed.
        per_engine: dict[str, int] = {}
        for s in dispatch_specs:
            name = s.engine or parent.engine_name
            per_engine[name] = per_engine.get(name, 0) + 1
        for engine_name, count in per_engine.items():
            capabilities = (
                getattr(parent.engine, "capabilities", None)
                if engine_name == parent.engine_name
                else _capabilities_of(engine_name)
            )
            warn_unenforceable_budget(capabilities, engine=engine_name, cap_usd=cap, workers=count)

    announce()
    workers = await dispatch_workers(
        parent,
        dispatch_specs,
        max_parallel=spec.max_parallel,
        reporter=reporter,
    )
    if budget is not None:
        for w in workers:
            budget.record(
                w.id,
                w.usage.cost_usd or 0.0,
                "sdk" if w.usage.cost_usd is not None else "estimated",
            )
    summary = budget.summary() if budget is not None else None
    if reporter is not None:
        reporter.wave_ended(
            dispatched=len(specs),
            unmatched=list(unmatched) or None,
            budget=summary,
        )
    return RosterRunResult(
        orchestrator_text=orchestrator_text,
        dispatched=len(specs),
        workers=tuple(workers),
        unmatched=tuple(unmatched),
        budget=summary,
    )


def _capabilities_of(engine_name: str) -> Any:
    """Best-effort capabilities lookup for an engine a worker named itself."""
    try:
        from ..engines import resolve_engine

        return getattr(resolve_engine(engine_name), "capabilities", None)
    except Exception:  # noqa: BLE001 — an unknown engine fails later, with a better message
        return None


__all__ = [
    "RosterRunResult",
    "dispatch_roster",
    "dispatch_roster_entries",
    "direct_answer_reason",
    "gate_applies",
    "match_crew",
    "no_reports_to_fold",
    "synthesis_enabled",
    "wave_needs_synthesis",
]
