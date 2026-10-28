"""Fair-share spend across a wave of concurrent workers."""

from __future__ import annotations

import logging

import pytest

from computeragent.orchestration.budget import (
    MIN_VIABLE_USD,
    BudgetAllocator,
    BudgetExceeded,
    warn_unenforceable_budget,
)
from computeragent.protocol_types import EngineCapabilities


def caps(*, budget: bool) -> EngineCapabilities:
    return EngineCapabilities(
        streaming_input=True,
        partial_messages=True,
        permission_callback=True,
        sessions=True,
        budget=budget,
    )


def test_the_slice_is_the_fair_share_not_the_whole_remainder():
    # Four in-flight workers each handed the full remainder would let the stage
    # overrun 4x while every worker stayed inside its own cap.
    assert BudgetAllocator(8.0).slice(4) == pytest.approx(2.0)


def test_the_per_worker_ceiling_still_applies_to_a_narrow_wave():
    assert BudgetAllocator(100.0, per_worker_usd=4.0).slice(1) == pytest.approx(4.0)


def test_a_wave_of_zero_is_treated_as_one():
    assert BudgetAllocator(1.0).slice(0) == pytest.approx(1.0)


def test_an_underfunded_wave_is_refused_before_any_spend():
    allocator = BudgetAllocator(0.10)
    with pytest.raises(BudgetExceeded) as exc:
        allocator.check_before_dispatch(10)
    assert "below the $0.05 minimum" in str(exc.value)
    assert allocator.spent == 0


def test_a_viable_wave_returns_its_cap():
    assert BudgetAllocator(1.0).check_before_dispatch(4) == pytest.approx(0.25)


def test_spend_accumulates_and_remaining_never_goes_negative():
    allocator = BudgetAllocator(1.0)
    allocator.record("w1", 0.6)
    allocator.record("w2", 0.9)
    assert allocator.spent == pytest.approx(1.5)
    assert allocator.remaining() == 0.0
    assert allocator.exhausted() is True


def test_a_negative_settlement_is_clamped_to_zero():
    allocator = BudgetAllocator(1.0)
    allocator.record("w1", -5.0)
    assert allocator.spent == 0.0


def test_estimated_share_reports_how_much_of_the_spend_was_a_guess():
    allocator = BudgetAllocator(10.0)
    allocator.record("w1", 1.0, "sdk")
    allocator.record("w2", 3.0, "estimated")
    assert allocator.estimated_share == pytest.approx(0.75)


def test_estimated_share_of_no_spend_is_zero_not_a_division_error():
    assert BudgetAllocator(10.0).estimated_share == 0.0


def test_the_summary_rounds_and_carries_every_settlement():
    allocator = BudgetAllocator(1.0)
    allocator.record("w1", 0.123456789)
    summary = allocator.summary()
    assert summary.spent_usd == pytest.approx(0.1235, abs=1e-9)
    assert [s.worker_id for s in summary.settlements] == ["w1"]


def test_exhausted_is_keyed_on_the_minimum_viable_amount():
    allocator = BudgetAllocator(MIN_VIABLE_USD * 2)
    assert allocator.exhausted() is False
    allocator.record("w1", MIN_VIABLE_USD * 1.5)
    assert allocator.exhausted() is True


def test_an_engine_that_cannot_enforce_the_cap_says_so(caplog):
    with caplog.at_level(logging.WARNING, logger="computeragent.orchestration.budget"):
        warn_unenforceable_budget(caps(budget=False), engine="gitagent", cap_usd=0.25, workers=4)
    assert "budget_unenforceable" in caplog.text
    assert "gitagent" in caplog.text


def test_an_engine_that_can_enforce_it_stays_quiet(caplog):
    with caplog.at_level(logging.WARNING, logger="computeragent.orchestration.budget"):
        warn_unenforceable_budget(caps(budget=True), engine="claude", cap_usd=0.25, workers=4)
        warn_unenforceable_budget(None, engine="unknown", cap_usd=0.25, workers=4)
    assert caplog.text == ""
