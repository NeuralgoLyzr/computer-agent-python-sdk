"""Regression coverage for the ``agent_description`` plumbing in 0.1.2.

The description threads through TelemetryEvent → OtelSink (invoke_agent span
attribute). (Its third hop — the AgentOS Mongo registry doc + inline manifest —
now lives in the agentos-server projection, covered by that package's tests.)
Tests lock that the value supplied via ``options.agent_description`` survives.
"""

from __future__ import annotations

import pytest

opentelemetry = pytest.importorskip("opentelemetry")
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from computeragent._proxy.query import (  # noqa: E402
    _derive_agent_description,
    _derive_agent_name,
)
from computeragent.telemetry import TelemetryEvent  # noqa: E402
from computeragent.telemetry.sinks import OtelSink  # noqa: E402

# ── proxy derivation ─────────────────────────────────────────────────────


class _FakeOptions:
    """Minimal stand-in for ClaudeAgentOptions — we only need attribute
    access here. The proxy reads ``agent_name`` / ``agent_description``
    via getattr, exactly like upstream's ClaudeAgentOptions allows."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_derive_agent_description_returns_none_when_unset():
    opts = _FakeOptions(system_prompt="You are NordAssist...")
    assert _derive_agent_description(opts) is None


def test_derive_agent_description_returns_string_when_set():
    opts = _FakeOptions(agent_description="QA assistant for engineers.")
    assert _derive_agent_description(opts) == "QA assistant for engineers."


def test_derive_agent_description_does_not_fall_back_to_system_prompt():
    """Unlike name, description has no implicit fallback — it's a human
    label, not something to synthesize from the prompt."""
    opts = _FakeOptions(system_prompt="You are NordAssist, an AI assistant...")
    assert _derive_agent_description(opts) is None


def test_derive_agent_description_ignores_non_string():
    opts = _FakeOptions(agent_description=123)
    assert _derive_agent_description(opts) is None


def test_derive_agent_name_unchanged_when_description_present():
    """0.1.2 must not break the existing name derivation. A description
    alone shouldn't pre-empt the name fallback."""
    opts = _FakeOptions(
        agent_description="…",
        system_prompt="You are NordAssist...",
    )
    assert _derive_agent_name(opts) == "You are NordAssist..."


# ── TelemetryEvent ───────────────────────────────────────────────────────


def test_telemetry_event_carries_description():
    ev = TelemetryEvent.session_started(
        session_id="sess_x",
        agent_name="QA-Bot",
        agent_description="NordAssist QA assistant",
        options=_FakeOptions(model="m", system_prompt="..."),
        prompt="What is X?",
    )
    assert ev.agent_name == "QA-Bot"
    assert ev.agent_description == "NordAssist QA assistant"


def test_telemetry_event_description_optional_default_none():
    """Existing callers that don't pass `agent_description` keep working
    bit-for-bit."""
    ev = TelemetryEvent.session_started(
        session_id="sess_x",
        agent_name="QA-Bot",
        options=_FakeOptions(model="m", system_prompt="..."),
        prompt="What is X?",
    )
    assert ev.agent_description is None


# ── OtelSink — gen_ai.agent.description on invoke_agent ──────────────────


def _build_inmemory_otel() -> tuple[OtelSink, InMemorySpanExporter]:
    span_exp = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exp))
    sink = OtelSink(tracer_provider=tracer_provider)
    return sink, span_exp


@pytest.mark.asyncio
async def test_otel_invoke_agent_carries_description():
    sink, exp = _build_inmemory_otel()
    await sink.emit(
        TelemetryEvent(
            kind="session_started",
            session_id="sess_o",
            agent_name="QA-Bot",
            agent_description="Pytest specialist",
            payload={"model": "claude-sonnet-4-5", "prompt": "q"},
        )
    )
    await sink.emit(
        TelemetryEvent(
            kind="session_ended",
            session_id="sess_o",
            agent_name="QA-Bot",
            agent_description="Pytest specialist",
            payload={"is_error": False, "duration_ms": 100.0},
        )
    )

    [invoke] = [s for s in exp.get_finished_spans() if s.name.startswith("invoke_agent")]
    attrs = dict(invoke.attributes or {})
    assert attrs.get("gen_ai.agent.name") == "QA-Bot"
    assert attrs.get("gen_ai.agent.description") == "Pytest specialist"


@pytest.mark.asyncio
async def test_otel_invoke_agent_omits_description_when_unset():
    """Backwards-compat — invoke_agent spans without a description don't
    grow a new attribute slot."""
    sink, exp = _build_inmemory_otel()
    await sink.emit(
        TelemetryEvent(
            kind="session_started",
            session_id="sess_o",
            agent_name="QA-Bot",
            payload={"model": "claude-sonnet-4-5", "prompt": "q"},
        )
    )
    await sink.emit(
        TelemetryEvent(
            kind="session_ended",
            session_id="sess_o",
            agent_name="QA-Bot",
            payload={"is_error": False, "duration_ms": 100.0},
        )
    )

    [invoke] = [s for s in exp.get_finished_spans() if s.name.startswith("invoke_agent")]
    attrs = dict(invoke.attributes or {})
    assert "gen_ai.agent.description" not in attrs
