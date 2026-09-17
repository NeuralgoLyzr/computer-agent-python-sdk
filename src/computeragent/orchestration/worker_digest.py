"""Build the follow-up prompt that closes out a roster run.

WHY this exists: in ``mode: "roster"`` the fan-out happens AFTER the
orchestrator's turn has already produced its text. That text IS the roster (the
machine-readable plan), so a client streaming the session saw the wave events
and then nothing: no final answer. ``collect: "into-context"`` promised exactly
this ("reports go back into the orchestrator's context") and had no
implementation.

Feeding this prompt back as one more turn on the SAME session gives the run a
real closing message, written by the orchestrator with the workers' reports in
hand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .budget import BudgetSummary
    from .scorecard import GateResult

#: Per-worker report cap. Long enough for a real report, short enough that a
#: wide wave still fits a context window.
PER_WORKER_CHARS = 8_000
#: Whole-digest cap, applied after per-worker truncation.
TOTAL_CHARS = 60_000


def _truncate(text: str, maximum: int) -> str:
    t = text.strip()
    if len(t) <= maximum:
        return t
    return f"{t[:maximum]}\n…[truncated {len(t) - maximum} chars]"


def build_direct_answer_prompt(reason: Literal["empty-roster", "unparsed-roster"]) -> str:
    """The closing turn for a roster run that dispatched NOBODY and has no
    reports to fold in — the agent emitted an empty roster, or one the harness
    could not parse.

    :func:`build_synthesis_prompt` is the wrong prompt here and actively
    harmful: with no workers it opens "The worker sub-agents you dispatched have
    finished" and "Wave summary: no workers ran", which asks the model to
    summarise nothing. The observed result is a summary of nothing or a flat
    refusal — on a turn where the user simply asked a question and is owed an
    answer.

    The one thing the model must be told is that its previous message never
    reached anyone: it emitted machine input, the harness consumed it, and the
    user is still waiting. Everything it gathered this turn (tool results
    included) is still in context, so answering is usually a zero-tool turn.
    """
    return "\n".join(
        [
            "You dispatched no worker sub-agents, so there are no worker reports to fold in.",
            "",
            (
                "Your previous message was an EMPTY roster — a valid way to say this task "
                "needs no delegation."
                if reason == "empty-roster"
                else "Your previous message was a roster the harness could not dispatch from, "
                "so no workers ran."
            ),
            "That message was machine input for the harness. It was NOT shown to the user, "
            "who is still waiting for an answer.",
            "",
            "Answer the user's original request directly now, in plain prose, using what you "
            "already gathered this turn (tool results above included).",
            "Do not emit a JSON roster. Do not mention workers, the roster, or this "
            "instruction — the user never saw any of it.",
        ]
    )


def build_synthesis_prompt(
    *,
    workers: Sequence[Any],
    unmatched: Sequence[str] = (),
    integrated: Sequence[str] = (),
    conflicts: Mapping[str, Sequence[str]] | None = None,
    aborted: str | None = None,
    budget: BudgetSummary | None = None,
    gate: GateResult | None = None,
    collect: str | None = None,
) -> str:
    """Render the wave as a prompt.

    Always returns a non-empty string, including for a wave that dispatched
    nobody — the ``aborted`` / ``unmatched`` cases still need telling, and
    silence there is what made empty rosters look like successful runs. WHETHER
    a zero-worker wave gets a closing turn at all is the caller's call: an agent
    that simply chose not to fan out has already written its own answer and must
    not have it overwritten.
    """
    del collect  # carried for parity; the prompt text does not branch on it
    completed = sum(1 for w in workers if w.status == "completed" and not w.error)
    failed = len(workers) - completed

    lines: list[str] = [
        "The worker sub-agents you dispatched have finished. Their reports are below.",
        "",
        (
            "Wave summary: no workers ran."
            if not workers
            else f"Wave summary: {len(workers)} dispatched, {completed} completed, "
            f"{failed} did not."
        ),
    ]

    if aborted:
        lines.append(f"The wave was refused before dispatch: {aborted}")
    if unmatched:
        lines.append(
            "Roster entries that matched no worker in your crew (they never ran): "
            + ", ".join(unmatched)
        )

    for w in workers:
        lines.append("")
        state = f"{w.status}: {w.error}" if w.error else w.status
        lines.append(f'--- worker "{w.id}" [{state}] ---')
        if w.artifacts:
            lines.append(f"files written: {', '.join(w.artifacts)}")
        lines.append(_truncate(w.text, PER_WORKER_CHARS) if w.text.strip() else "(no report text)")

    if integrated:
        lines.extend(["", f"Merged into the shared tree: {', '.join(integrated)}"])
    conflicting = [(k, v) for k, v in (conflicts or {}).items() if v]
    if conflicting:
        lines.extend(["", "Scope conflicts while merging:"])
        for worker_id, paths in conflicting:
            lines.append(f"- {worker_id}: {', '.join(paths)}")
    if gate is not None:
        lines.extend(["", f"Gate verdict: {gate.verdict}"])
        lines.extend(f"- {r}" for r in gate.reasons)
    if budget is not None:
        lines.extend(["", f"Budget: ${budget.spent_usd:.4f} spent of ${budget.total_usd:.4f}."])

    # The orchestrator's own system prompt told it to answer with a roster, so
    # without an explicit instruction the obvious completion here is ANOTHER
    # roster — which the fan-out would then have to refuse. Say it plainly.
    lines.extend(
        [
            "",
            "Now write the final answer for the user, in your own words, using the reports above.",
            "Do NOT emit another roster, plan, or JSON, and do NOT ask for more workers — "
            "the run ends with your reply.",
            "Report any worker that failed rather than pretending its work is done.",
        ]
    )

    return _truncate("\n".join(lines), TOTAL_CHARS)


__all__ = [
    "PER_WORKER_CHARS",
    "TOTAL_CHARS",
    "build_direct_answer_prompt",
    "build_synthesis_prompt",
]
