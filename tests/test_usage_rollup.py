"""Coverage for ``UsageRollup.absorb`` — folds delta vs cumulative
``EngineEvent(kind='usage_snapshot')`` events into the running totals.
"""

from __future__ import annotations

from pytest import approx

from computeragent.harness.usage import UsageRollup
from computeragent.protocol_types import EngineEvent


def _snap(**kw) -> EngineEvent:
    return EngineEvent(kind="usage_snapshot", **kw)


def test_delta_events_sum():
    rollup = UsageRollup()
    rollup.absorb(_snap(input_tokens=10, output_tokens=2, cost_usd=0.001, cost_semantic="delta"))
    rollup.absorb(_snap(input_tokens=5, output_tokens=8, cost_usd=0.002, cost_semantic="delta"))
    assert rollup.input_tokens == 15
    assert rollup.output_tokens == 10
    assert rollup.cost_usd == approx(0.003)


def test_cumulative_events_overwrite():
    rollup = UsageRollup()
    rollup.absorb(
        _snap(
            input_tokens=10,
            output_tokens=2,
            cost_usd=0.001,
            cost_semantic="cumulative",
        )
    )
    rollup.absorb(
        _snap(
            input_tokens=25,
            output_tokens=15,
            cost_usd=0.010,
            cost_semantic="cumulative",
        )
    )
    # Later cumulative wins — not the sum.
    assert rollup.input_tokens == 25
    assert rollup.output_tokens == 15
    assert rollup.cost_usd == approx(0.010)


def test_unspecified_semantic_treated_as_delta():
    rollup = UsageRollup()
    rollup.absorb(_snap(input_tokens=3, output_tokens=4))
    rollup.absorb(_snap(input_tokens=2, output_tokens=1))
    assert rollup.input_tokens == 5
    assert rollup.output_tokens == 5


def test_cache_fields_carry_through_delta():
    rollup = UsageRollup()
    rollup.absorb(
        _snap(
            cache_creation_input_tokens=4,
            cache_read_input_tokens=10,
            cost_semantic="delta",
        )
    )
    assert rollup.cache_creation_input_tokens == 4
    assert rollup.cache_read_input_tokens == 10


def test_non_usage_events_are_ignored():
    rollup = UsageRollup()
    rollup.absorb(EngineEvent(kind="sdk_message", payload={"x": 1}))
    assert rollup.input_tokens == 0
    assert rollup.cost_usd == 0.0
