"""The two closing-turn prompts, and the three decisions that pick between
them — or skip the turn entirely."""

from __future__ import annotations

from computeragent.orchestration.budget import BudgetSummary
from computeragent.orchestration.roster_run import (
    RosterRunResult,
    direct_answer_reason,
    gate_applies,
    no_reports_to_fold,
    wave_needs_synthesis,
)
from computeragent.orchestration.scorecard import GateResult
from computeragent.orchestration.spawn_child import WorkerResult, WorkerUsage
from computeragent.orchestration.worker_digest import (
    PER_WORKER_CHARS,
    build_direct_answer_prompt,
    build_synthesis_prompt,
)


def worker(wid="w1", *, status="completed", error=None, text="did the thing", artifacts=()):
    return WorkerResult(
        id=wid,
        session_id=f"sess_{wid}",
        status=status,
        text=text,
        usage=WorkerUsage(input_tokens=1, output_tokens=1, cost_usd=0.5),
        error=error,
        artifacts=tuple(artifacts),
    )


# ── the synthesis prompt ───────────────────────────────────────────────────


def test_it_counts_completions_and_failures():
    prompt = build_synthesis_prompt(
        workers=[worker("w1"), worker("w2", status="errored", error="boom")]
    )
    assert "2 dispatched, 1 completed, 1 did not" in prompt


def test_each_worker_gets_its_own_block_with_its_state():
    prompt = build_synthesis_prompt(workers=[worker("w2", status="errored", error="boom")])
    assert '--- worker "w2" [errored: boom] ---' in prompt


def test_a_worker_with_no_text_says_so_rather_than_leaving_a_gap():
    assert "(no report text)" in build_synthesis_prompt(workers=[worker(text="   ")])


def test_artifacts_are_listed_per_worker():
    prompt = build_synthesis_prompt(workers=[worker(artifacts=["src/a.py", "src/b.py"])])
    assert "files written: src/a.py, src/b.py" in prompt


def test_a_long_report_is_truncated_with_the_count_it_dropped():
    prompt = build_synthesis_prompt(workers=[worker(text="x" * (PER_WORKER_CHARS + 50))])
    assert "…[truncated 50 chars]" in prompt


def test_an_abort_is_reported_even_though_nobody_ran():
    prompt = build_synthesis_prompt(workers=[], aborted="not enough budget")
    assert "Wave summary: no workers ran." in prompt
    assert "refused before dispatch: not enough budget" in prompt


def test_unmatched_entries_are_named_as_a_config_error():
    prompt = build_synthesis_prompt(workers=[], unmatched=["typo-worker"])
    assert "matched no worker in your crew" in prompt
    assert "typo-worker" in prompt


def test_merge_results_and_conflicts_ride_on_the_wave_not_a_worker():
    prompt = build_synthesis_prompt(
        workers=[worker()],
        integrated=["src/a.py"],
        conflicts={"w2": ["src/a.py"], "w3": []},
    )
    assert "Merged into the shared tree: src/a.py" in prompt
    assert "- w2: src/a.py" in prompt
    assert "w3" not in prompt.split("Scope conflicts")[1]


def test_the_gate_verdict_and_budget_are_included_when_present():
    prompt = build_synthesis_prompt(
        workers=[worker()],
        gate=GateResult(verdict="fail", reasons=("worker w2 conflicts",)),
        budget=BudgetSummary(
            total_usd=4.0,
            spent_usd=1.25,
            remaining_usd=2.75,
            per_worker_cap_usd=1.0,
            estimated_share=0.0,
        ),
    )
    assert "Gate verdict: fail" in prompt
    assert "- worker w2 conflicts" in prompt
    assert "Budget: $1.2500 spent of $4.0000." in prompt


def test_it_forbids_another_roster_because_the_prompt_asked_for_one():
    prompt = build_synthesis_prompt(workers=[worker()])
    assert "Do NOT emit another roster" in prompt
    assert "Report any worker that failed" in prompt


# ── the direct-answer prompt ───────────────────────────────────────────────


def test_the_empty_roster_flavour_says_the_decline_was_valid():
    prompt = build_direct_answer_prompt("empty-roster")
    assert "EMPTY roster" in prompt
    assert "valid way to say this task needs no delegation" in prompt


def test_the_unparsed_flavour_says_the_harness_could_not_dispatch():
    assert "could not dispatch from" in build_direct_answer_prompt("unparsed-roster")


def test_both_flavours_tell_the_model_its_last_message_was_never_shown():
    for reason in ("empty-roster", "unparsed-roster"):
        prompt = build_direct_answer_prompt(reason)
        assert "It was NOT shown to the user" in prompt
        assert "Do not emit a JSON roster" in prompt


# ── the decisions ──────────────────────────────────────────────────────────


def test_a_prose_answer_with_no_workers_keeps_its_own_text():
    # Synthesising here would spend a call AND replace a good answer with a
    # summary of nothing.
    assert wave_needs_synthesis(RosterRunResult(), "no-block") is False
    assert wave_needs_synthesis(RosterRunResult(), "not-a-roster") is False


def test_roster_text_always_takes_the_closing_turn():
    for outcome in ("empty-roster", "no-usable-entries", "invalid-json"):
        assert wave_needs_synthesis(RosterRunResult(), outcome) is True


def test_an_abort_or_an_unmatched_entry_forces_the_turn_on_its_own():
    assert wave_needs_synthesis(RosterRunResult(aborted="broke"), "no-block") is True
    assert wave_needs_synthesis(RosterRunResult(unmatched=("x",)), "no-block") is True


def test_any_worker_at_all_forces_the_turn():
    assert wave_needs_synthesis(RosterRunResult(workers=(worker(),)), "no-block") is True


def test_only_an_empty_roster_escapes_the_gate():
    assert gate_applies("empty-roster") is False
    for outcome in ("entries", "no-usable-entries", "invalid-json", "no-block", "not-a-roster"):
        assert gate_applies(outcome) is True


def test_no_reports_to_fold_picks_the_direct_answer_prompt():
    assert no_reports_to_fold(RosterRunResult()) is True
    assert no_reports_to_fold(RosterRunResult(workers=(worker(),))) is False
    assert no_reports_to_fold(RosterRunResult(aborted="x")) is False
    assert no_reports_to_fold(RosterRunResult(unmatched=("x",))) is False


def test_the_direct_answer_reason_distinguishes_a_decline_from_a_breakage():
    assert direct_answer_reason("empty-roster") == "empty-roster"
    assert direct_answer_reason("invalid-json") == "unparsed-roster"
