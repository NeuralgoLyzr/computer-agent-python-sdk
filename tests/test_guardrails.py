"""``GuardrailFilter`` — truncation, tool allowlist (glob), cost ceiling, content filter."""

from __future__ import annotations

import pytest

from computeragent.telemetry import GuardrailFilter, TelemetryEvent


def _ev(kind, payload, sid="s1"):
    return TelemetryEvent(kind=kind, session_id=sid, agent_name=None, payload=payload)


@pytest.mark.asyncio
async def test_truncates_long_string_values():
    g = GuardrailFilter(max_attribute_length=10, tool_name_allowlist=None)
    out = await g(_ev("assistant_message", {"text": "x" * 100}))
    assert out is not None
    assert out.payload["text"].startswith("x" * 10)
    assert "+90 chars" in out.payload["text"]


@pytest.mark.asyncio
async def test_tool_name_allowlist_literal():
    g = GuardrailFilter(max_attribute_length=None, tool_name_allowlist={"Read", "Glob"})
    allowed = await g(_ev("tool_use", {"tool_name": "Read", "tool_use_id": "1"}))
    blocked = await g(_ev("tool_use", {"tool_name": "Bash", "tool_use_id": "2"}))
    assert allowed is not None
    assert blocked is None


@pytest.mark.asyncio
async def test_tool_name_allowlist_glob():
    g = GuardrailFilter(max_attribute_length=None, tool_name_allowlist={"mcp__acme-assist-tools__*"})
    a = await g(
        _ev("tool_use", {"tool_name": "mcp__acme-assist-tools__lookup_team", "tool_use_id": "1"})
    )
    b = await g(_ev("tool_use", {"tool_name": "mcp__other__thing", "tool_use_id": "2"}))
    assert a is not None
    assert b is None


@pytest.mark.asyncio
async def test_cost_ceiling_trips_and_drops_subsequent_events():
    g = GuardrailFilter(max_attribute_length=None, cost_ceiling_usd=0.5)
    # First snapshot under the cap — passes through.
    pre = await g(_ev("usage_snapshot", {"total_cost_usd": 0.1, "usage": {}}))
    assert pre is not None
    assert pre.kind == "usage_snapshot"

    # Snapshot over the cap — transformed to budget_exceeded session_ended.
    trip = await g(_ev("usage_snapshot", {"total_cost_usd": 0.6, "usage": {}}))
    assert trip is not None
    assert trip.kind == "session_ended"
    assert trip.payload["subtype"] == "budget_exceeded"

    # Subsequent non-session-ended event from the same session — dropped.
    follow = await g(_ev("assistant_message", {"text": "more text"}))
    assert follow is None

    # session_ended for that session — forwarded so consumers see closure.
    end = await g(_ev("session_ended", {"is_error": False, "subtype": "success"}))
    # After the trip the in-memory state was cleared on the synthetic
    # session_ended emit; an additional real session_ended for the same
    # session passes through normally.
    assert end is not None


@pytest.mark.asyncio
async def test_content_filter_runs_last_and_can_drop():
    captured: list[str] = []

    async def cf(ev):
        captured.append(ev.kind)
        return  # drop

    g = GuardrailFilter(max_attribute_length=None, content_filter=cf)
    out = await g(_ev("assistant_message", {"text": "hi"}))
    assert out is None
    assert captured == ["assistant_message"]


@pytest.mark.asyncio
async def test_content_filter_can_transform():
    async def cf(ev):
        ev.payload["text"] = "TRANSFORMED"
        return ev

    g = GuardrailFilter(max_attribute_length=None, content_filter=cf)
    out = await g(_ev("assistant_message", {"text": "hi"}))
    assert out is not None
    assert out.payload["text"] == "TRANSFORMED"


@pytest.mark.asyncio
async def test_cost_ceiling_isolated_per_session():
    g = GuardrailFilter(max_attribute_length=None, cost_ceiling_usd=0.5)
    over = await g(_ev("usage_snapshot", {"total_cost_usd": 0.7, "usage": {}}, sid="A"))
    assert over is not None and over.kind == "session_ended"
    # Other session unaffected.
    other = await g(_ev("usage_snapshot", {"total_cost_usd": 0.1, "usage": {}}, sid="B"))
    assert other is not None and other.kind == "usage_snapshot"
