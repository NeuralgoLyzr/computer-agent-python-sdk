"""Pipeline behaviors: middleware ordering, drop, fail-closed, fan-out."""

from __future__ import annotations

import pytest

from computeragent.telemetry import Pipeline, TelemetryEvent


def _event(kind="assistant_message", payload=None):
    return TelemetryEvent(kind=kind, session_id="s1", agent_name="a", payload=payload or {})


@pytest.mark.asyncio
async def test_middleware_runs_in_order():
    seen: list[str] = []

    async def mw_a(ev):
        seen.append("a")
        return ev

    async def mw_b(ev):
        seen.append("b")
        return ev

    p = Pipeline(middleware=[mw_a, mw_b])
    await p.emit(_event())
    await p.flush()
    assert seen == ["a", "b"]


@pytest.mark.asyncio
async def test_returning_none_drops_event(stub_sink):
    async def drop(ev):
        return None

    p = Pipeline(middleware=[drop], sinks=[stub_sink])
    await p.emit(_event())
    await p.flush()
    assert stub_sink.events == []


@pytest.mark.asyncio
async def test_middleware_exception_drops_by_default(stub_sink):
    async def boom(ev):
        raise RuntimeError("nope")

    p = Pipeline(middleware=[boom], sinks=[stub_sink], fail_open=False)
    await p.emit(_event())
    await p.flush()
    assert stub_sink.events == []  # fail-closed


@pytest.mark.asyncio
async def test_middleware_exception_forwards_when_fail_open(stub_sink):
    async def boom(ev):
        raise RuntimeError("nope")

    p = Pipeline(middleware=[boom], sinks=[stub_sink], fail_open=True)
    await p.emit(_event())
    await p.flush()
    assert len(stub_sink.events) == 1


@pytest.mark.asyncio
async def test_multiple_sinks_fan_out(stub_sink):
    class Counter:
        def __init__(self):
            self.n = 0

        async def emit(self, _ev):
            self.n += 1

        async def flush(self, timeout=5.0):
            pass

        async def aclose(self):
            pass

    c = Counter()
    p = Pipeline(sinks=[stub_sink, c])
    await p.emit(_event())
    await p.emit(_event(kind="usage_snapshot"))
    await p.flush()
    assert len(stub_sink.events) == 2
    assert c.n == 2


@pytest.mark.asyncio
async def test_flush_awaits_pending_sink_tasks():
    import asyncio

    completed: list[bool] = []

    class Sluggish:
        async def emit(self, _ev):
            await asyncio.sleep(0.02)
            completed.append(True)

        async def flush(self, timeout=5.0):
            pass

        async def aclose(self):
            pass

    p = Pipeline(sinks=[Sluggish()])
    await p.emit(_event())
    # Without awaiting flush, the sink task may not have completed yet.
    await p.flush()
    assert completed == [True]


@pytest.mark.asyncio
async def test_sink_emit_exception_does_not_propagate(stub_sink):
    class Bad:
        async def emit(self, _ev):
            raise RuntimeError("yikes")

        async def flush(self, timeout=5.0):
            pass

        async def aclose(self):
            pass

    p = Pipeline(sinks=[Bad(), stub_sink])
    await p.emit(_event())
    await p.flush()
    # The good sink still got the event.
    assert len(stub_sink.events) == 1
