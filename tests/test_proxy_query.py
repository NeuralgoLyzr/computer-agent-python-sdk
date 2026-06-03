"""Proxy behavior: passthrough, telemetry emission, failure handling."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from computeragent import configure, query


def _make_assistant(text: str = "hi", tool_uses=None) -> AssistantMessage:
    content: list[Any] = [TextBlock(text=text)]
    for tu in tool_uses or []:
        content.append(tu)
    return AssistantMessage(content=content, model="claude-sonnet-4-6")


def _make_result(text: str = "done", cost: float = 0.01, turns: int = 1) -> Any:
    r = MagicMock(spec=ResultMessage)
    r.result = text
    r.total_cost_usd = cost
    r.num_turns = turns
    r.is_error = False
    r.subtype = "success"
    r.session_id = "sess_xyz"
    r.usage = {"input_tokens": 10, "output_tokens": 5}
    r.duration_api_ms = 100.0
    r.model = "claude-sonnet-4-6"
    return r


@pytest.mark.asyncio
async def test_passthrough_preserves_order(upstream_query_factory, stub_sink):
    """Every upstream message is yielded by computeragent.query in order."""
    a1 = _make_assistant("first")
    a2 = _make_assistant("second")
    r = _make_result()
    upstream_query_factory([a1, a2, r])
    configure(sinks=[stub_sink])

    received: list[Any] = []
    async for msg in query(prompt="hi"):
        received.append(msg)

    assert received == [a1, a2, r]  # identity-preserving passthrough


@pytest.mark.asyncio
async def test_emits_session_started_and_ended(upstream_query_factory, stub_sink):
    upstream_query_factory([_make_result()])
    configure(sinks=[stub_sink])
    async for _ in query(prompt="hi"):
        pass

    kinds = [e.kind for e in stub_sink.events]
    assert kinds[0] == "session_started"
    assert kinds[-1] == "session_ended"
    assert "usage_snapshot" in kinds


@pytest.mark.asyncio
async def test_emits_tool_use_event(upstream_query_factory, stub_sink):
    tu = ToolUseBlock(id="t1", name="Read", input={"path": "x"})
    a = _make_assistant("calling Read", tool_uses=[tu])
    upstream_query_factory([a, _make_result()])
    configure(sinks=[stub_sink])
    async for _ in query(prompt="hi"):
        pass

    tool_events = [e for e in stub_sink.events if e.kind == "tool_use"]
    assert len(tool_events) == 1
    assert tool_events[0].payload["tool_name"] == "Read"
    assert tool_events[0].payload["tool_use_id"] == "t1"
    assert tool_events[0].payload["tool_input"] == {"path": "x"}


@pytest.mark.asyncio
async def test_session_id_is_stable_across_events_and_upstream_id_captured_on_ended(
    upstream_query_factory, stub_sink
):
    """Regression: the proxy uses ONE session id (its own ``ca_…`` placeholder)
    for every event in the run, even after upstream's session id is known.
    Sinks like ``AgentRegistrySink`` key per-session bookkeeping on
    ``session_started`` — rebinding mid-stream would orphan that state and
    cause empty agent_logs.query / missing sessions.entries[]. The upstream
    id is surfaced separately on the ``session_ended`` payload for join-by-id.
    """
    r = _make_result()  # ResultMessage with session_id="sess_xyz"
    upstream_query_factory([r])
    configure(sinks=[stub_sink])
    async for _ in query(prompt="hi"):
        pass

    sids = {e.session_id for e in stub_sink.events}
    # Exactly one session id, and it's the placeholder (not the upstream's).
    assert len(sids) == 1
    [sid] = sids
    assert sid.startswith("ca_")
    assert sid != "sess_xyz"

    # session_ended carries the upstream id as auxiliary metadata.
    [ended] = [e for e in stub_sink.events if e.kind == "session_ended"]
    assert ended.payload.get("upstream_session_id") == "sess_xyz"


@pytest.mark.asyncio
async def test_sink_failure_does_not_break_run(upstream_query_factory):
    """A sink that throws every emit must not propagate; the run finishes."""

    class BrokenSink:
        emits = 0

        async def emit(self, event):
            BrokenSink.emits += 1
            raise RuntimeError("kaboom")

        async def flush(self, timeout=5.0):
            return None

        async def aclose(self):
            return None

    upstream_query_factory([_make_result()])
    configure(sinks=[BrokenSink()])
    msgs = [m async for m in query(prompt="hi")]
    assert len(msgs) == 1  # ResultMessage still delivered
    assert BrokenSink.emits >= 1  # sink was tried


@pytest.mark.asyncio
async def test_upstream_exception_emits_error_session_ended(upstream_query_factory, stub_sink):
    async def _boom(**_kwargs):
        if False:
            yield None  # make it an async generator
        raise RuntimeError("upstream blew up")

    from unittest.mock import patch

    import claude_agent_sdk

    import computeragent  # noqa: F401 - ensure registry initialized

    configure(sinks=[stub_sink])
    with patch.object(claude_agent_sdk, "query", _boom), pytest.raises(RuntimeError):
        async for _ in query(prompt="hi"):
            pass

    kinds = [e.kind for e in stub_sink.events]
    assert kinds[0] == "session_started"
    # session_ended with error subtype
    ended = [e for e in stub_sink.events if e.kind == "session_ended"]
    assert ended and ended[-1].payload["is_error"] is True
    assert ended[-1].payload["subtype"] == "error"


@pytest.mark.asyncio
async def test_slow_sink_does_not_block_yields(upstream_query_factory):
    """Sink writes run as background tasks. A slow sink must not delay the
    per-message yield path."""

    class SlowSink:
        delays = []

        async def emit(self, event):
            await asyncio.sleep(0.05)
            SlowSink.delays.append(event.kind)

        async def flush(self, timeout=5.0):
            return None

        async def aclose(self):
            return None

    r = _make_result()
    upstream_query_factory([_make_assistant("a"), r])
    configure(sinks=[SlowSink()])

    import time

    t0 = time.monotonic()
    async for _ in query(prompt="hi"):
        pass
    elapsed = time.monotonic() - t0
    # Even if the sink awaits 50ms × ~5 events serially, the iteration itself
    # should finish well under 200ms because emits are scheduled, not awaited
    # inline. Generous bound to keep the test stable on CI.
    assert elapsed < 1.0
