"""End-to-end tests for the harness with a fake engine.

We sidestep the real claude/gitagent engines here — those are exercised
by their dedicated test files. This file proves the coordinator + chat
handle + telemetry-fan-out plumbing works with any
:class:`EngineDriver`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from computeragent.engines import register_engine
from computeragent.harness import ChatResult, ComputerAgent, run_task
from computeragent.protocol_types import (
    EngineCapabilities,
    EngineContext,
    EngineEvent,
)
from computeragent.telemetry import TelemetryEvent
from computeragent.telemetry.pipeline import Pipeline


class _CapturingSink:
    """Test-only sink — records every event the coordinator emits."""

    def __init__(self) -> None:
        self.events: list[TelemetryEvent] = []

    async def emit(self, event: TelemetryEvent) -> None:
        self.events.append(event)

    async def flush(self, timeout: float = 5.0) -> None:  # noqa: ARG002
        return None

    async def aclose(self) -> None:
        return None


class _ScriptedEngine:
    """Engine that yields a deterministic sequence of events."""

    name = "scripted-test"
    capabilities = EngineCapabilities(
        streaming_input=True,
        partial_messages=False,
        permission_callback=True,
        sessions=True,
        budget=False,
    )

    def __init__(self, *, events: list[EngineEvent], record_ctx: dict[str, Any]) -> None:
        self._events = events
        self._record_ctx = record_ctx

    async def start_session(
        self, ctx: EngineContext
    ) -> AsyncIterator[EngineEvent]:
        self._record_ctx["ctx"] = ctx
        # Drain at least one user message so the harness queue mechanism
        # is exercised (and we can assert messages reached us).
        first_msg: Any = None
        async for entry in ctx.user_message_queue:
            first_msg = entry
            break
        self._record_ctx["first_user_msg"] = first_msg
        for ev in self._events:
            yield ev


def _scripted(events: list[EngineEvent], ctx_holder: dict[str, Any]) -> _ScriptedEngine:
    return _ScriptedEngine(events=events, record_ctx=ctx_holder)


@pytest.mark.asyncio
async def test_run_task_drains_to_chat_result(tmp_path: Path):
    pipeline = Pipeline(sinks=[])  # No sinks — just exercise the flow.
    ctx_holder: dict[str, Any] = {}
    engine = _scripted(
        events=[
            EngineEvent(kind="sdk_message", payload={"role": "assistant", "content": "hi"}),
            EngineEvent(
                kind="usage_snapshot",
                cost_semantic="delta",
                input_tokens=10,
                output_tokens=3,
                cost_usd=0.0,
            ),
        ],
        ctx_holder=ctx_holder,
    )
    register_engine("scripted-test", engine)

    result = await run_task(
        message="hello",
        source=tmp_path,
        engine="scripted-test",
        telemetry_pipeline=pipeline,
    )

    assert isinstance(result, ChatResult)
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 3
    assert len(result.messages) == 1
    assert ctx_holder["first_user_msg"] == {"role": "user", "content": "hello"}
    # workspace_path was passed through (caller-supplied Path).
    assert ctx_holder["ctx"].workspace_path == tmp_path


@pytest.mark.asyncio
async def test_telemetry_events_fire_in_expected_order(tmp_path: Path):
    sink = _CapturingSink()
    pipeline = Pipeline(sinks=[sink])
    engine = _scripted(
        events=[
            EngineEvent(kind="sdk_message", payload={"role": "assistant", "content": "ok"}),
            EngineEvent(
                kind="usage_snapshot",
                cost_semantic="cumulative",
                input_tokens=5,
                output_tokens=2,
                cost_usd=0.001,
            ),
        ],
        ctx_holder={},
    )
    register_engine("scripted-test", engine)

    await run_task(
        message="hi",
        source=tmp_path,
        engine="scripted-test",
        telemetry_pipeline=pipeline,
    )
    await pipeline.flush()

    kinds = [ev.kind for ev in sink.events]
    assert kinds[0] == "session_started"
    assert "usage_snapshot" in kinds
    assert kinds[-1] == "session_ended"


@pytest.mark.asyncio
async def test_policy_decider_is_threaded_into_context(tmp_path: Path):
    ctx_holder: dict[str, Any] = {}
    engine = _scripted(events=[], ctx_holder=ctx_holder)
    register_engine("scripted-test", engine)

    async def _decider(tool_name: str, tool_input: dict[str, Any], context: Any) -> dict:  # noqa: ARG001
        return {"behavior": "allow", "updated_input": tool_input}

    await run_task(
        message="probe",
        source=tmp_path,
        engine="scripted-test",
        policy_decider=_decider,
    )
    assert ctx_holder["ctx"].policy_decider is _decider


@pytest.mark.asyncio
async def test_unknown_engine_raises(tmp_path: Path):
    from computeragent.harness.errors import UnknownEngineError

    with pytest.raises(UnknownEngineError):
        await run_task(
            message="x",
            source=tmp_path,
            engine="does-not-exist",
        )


@pytest.mark.asyncio
async def test_computer_agent_chat_returns_handle_synchronously(tmp_path: Path):
    """`chat()` must return immediately, NOT block on the engine boot."""
    ctx_holder: dict[str, Any] = {}
    engine = _scripted(
        events=[EngineEvent(kind="sdk_message", payload={"role": "assistant", "content": "x"})],
        ctx_holder=ctx_holder,
    )
    register_engine("scripted-test", engine)

    async with ComputerAgent(source=tmp_path, engine="scripted-test") as agent:
        handle = agent.chat("hi")
        # ``chat()`` should be cheap — no awaits yet → no events captured.
        assert "ctx" not in ctx_holder
        result = await handle
        assert result.messages
        # Now the engine has run, context recorded.
        assert "ctx" in ctx_holder


@pytest.mark.asyncio
async def test_chat_handle_async_for_then_await_returns_same_result(tmp_path: Path):
    engine = _scripted(
        events=[
            EngineEvent(kind="sdk_message", payload={"role": "assistant", "content": "1"}),
            EngineEvent(kind="sdk_message", payload={"role": "assistant", "content": "2"}),
        ],
        ctx_holder={},
    )
    register_engine("scripted-test", engine)

    async with ComputerAgent(source=tmp_path, engine="scripted-test") as agent:
        handle = agent.chat("hi")
        collected: list[EngineEvent] = []
        async for ev in handle:
            collected.append(ev)
        result = await handle
    assert [ev.kind for ev in collected] == ["sdk_message", "sdk_message"]
    assert len(result.messages) == 2


@pytest.mark.asyncio
async def test_get_usage_returns_running_totals(tmp_path: Path):
    engine = _scripted(
        events=[
            EngineEvent(
                kind="usage_snapshot",
                cost_semantic="delta",
                input_tokens=1,
                output_tokens=2,
                cost_usd=0.5,
            ),
            EngineEvent(
                kind="usage_snapshot",
                cost_semantic="delta",
                input_tokens=3,
                output_tokens=4,
                cost_usd=0.5,
            ),
        ],
        ctx_holder={},
    )
    register_engine("scripted-test", engine)

    async with ComputerAgent(source=tmp_path, engine="scripted-test") as agent:
        handle = agent.chat("hi")
        await handle
        usage = handle.get_usage()
    # Both deltas summed: 1+3 input, 2+4 output, 0.5+0.5 cost.
    assert usage.input_tokens == 4
    assert usage.output_tokens == 6
    assert usage.cost_usd == 1.0
