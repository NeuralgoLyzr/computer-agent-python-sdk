"""The gate verdict and the scorecard it rides on."""

from __future__ import annotations

from computeragent.orchestration.budget import BudgetSummary
from computeragent.orchestration.scorecard import build_scorecard, evaluate_gate
from computeragent.orchestration.spawn_child import WorkerResult, WorkerUsage


def worker(wid="w1", *, status="completed", error=None, artifacts=(), cost=0.5):
    return WorkerResult(
        id=wid,
        session_id=f"sess_{wid}",
        status=status,
        text="",
        usage=WorkerUsage(cost_usd=cost),
        error=error,
        artifacts=tuple(artifacts),
    )


def test_a_clean_run_passes():
    gate = evaluate_gate(workers=[worker()], conflicts={})
    assert gate.verdict == "pass"
    assert gate.reasons == ()


def test_a_wave_that_dispatched_nobody_fails():
    # Without this the gate certified every silent roster failure as green:
    # with no workers there is nothing to complain about, so `reasons` stayed
    # empty and the verdict came back "pass" for a run that did nothing.
    gate = evaluate_gate(workers=[], conflicts={})
    assert gate.verdict == "fail"
    assert "no workers were dispatched" in gate.reasons[0]


def test_an_abort_is_its_own_reason_and_suppresses_the_zero_worker_one():
    gate = evaluate_gate(workers=[], conflicts={}, aborted="under-funded")
    assert gate.verdict == "fail"
    assert gate.reasons == ("budget aborted the wave: under-funded",)


def test_a_worker_that_errored_fails_the_gate():
    gate = evaluate_gate(
        workers=[worker("w1"), worker("w2", status="errored", error="boom")], conflicts={}
    )
    assert gate.verdict == "fail"
    assert "worker w2 did not complete cleanly: boom" in gate.reasons


def test_a_worker_that_completed_but_carries_an_error_still_fails():
    gate = evaluate_gate(workers=[worker("w1", error="partial")], conflicts={})
    assert gate.verdict == "fail"


def test_a_merge_conflict_is_reported_as_the_scope_overlap_it_is():
    gate = evaluate_gate(workers=[worker()], conflicts={"w1": ["src/a.py"]})
    assert gate.verdict == "fail"
    assert "worker w1 conflicts on src/a.py (scope overlap)" in gate.reasons


def test_the_scorecard_projects_each_worker_and_carries_the_gate():
    summary = BudgetSummary(
        total_usd=4.0,
        spent_usd=1.0,
        remaining_usd=3.0,
        per_worker_cap_usd=1.0,
        estimated_share=0.0,
    )
    card = build_scorecard(
        "orchestrator",
        workers=[worker("w1", artifacts=["src/a.py", "src/b.py"])],
        conflicts={},
        integrated=["src/a.py"],
        budget=summary,
    )
    assert card.agent == "orchestrator"
    assert card.status == "pass"
    assert card.gate.verdict == "pass"
    assert card.workers[0].artifacts == 2
    assert card.workers[0].cost_usd == 0.5
    assert card.integrated == ("src/a.py",)
    assert card.budget is summary


def test_the_scorecard_status_tracks_the_gate_verdict():
    card = build_scorecard("a", workers=[], conflicts={}, integrated=[])
    assert card.status == "fail"
