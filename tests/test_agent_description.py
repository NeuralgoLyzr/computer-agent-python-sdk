"""Regression coverage for the new ``agent_description`` plumbing in 0.1.2.

The description threads through three layers — TelemetryEvent →
AgentRegistrySink (Mongo doc + inline manifest) → OtelSink (invoke_agent
span attribute). Tests lock that the value supplied via
``options.agent_description`` survives all three.
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

mongomock_motor = pytest.importorskip("mongomock_motor")
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from computeragent.telemetry.sinks import AgentRegistrySink  # noqa: E402

# ── proxy derivation ─────────────────────────────────────────────────────


class _FakeOptions:
    """Minimal stand-in for ClaudeAgentOptions — we only need attribute
    access here. The proxy reads ``agent_name`` / ``agent_description``
    via getattr, exactly like upstream's ClaudeAgentOptions allows."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_derive_agent_description_returns_none_when_unset():
    opts = _FakeOptions(system_prompt="You are AcmeAssist...")
    assert _derive_agent_description(opts) is None


def test_derive_agent_description_returns_string_when_set():
    opts = _FakeOptions(agent_description="QA assistant for engineers.")
    assert _derive_agent_description(opts) == "QA assistant for engineers."


def test_derive_agent_description_does_not_fall_back_to_system_prompt():
    """Unlike name, description has no implicit fallback — it's a human
    label, not something to synthesize from the prompt."""
    opts = _FakeOptions(system_prompt="You are AcmeAssist, an AI assistant...")
    assert _derive_agent_description(opts) is None


def test_derive_agent_description_ignores_non_string():
    opts = _FakeOptions(agent_description=123)
    assert _derive_agent_description(opts) is None


def test_derive_agent_name_unchanged_when_description_present():
    """0.1.2 must not break the existing name derivation. A description
    alone shouldn't pre-empt the name fallback."""
    opts = _FakeOptions(
        agent_description="…",
        system_prompt="You are AcmeAssist...",
    )
    assert _derive_agent_name(opts) == "You are AcmeAssist..."


# ── TelemetryEvent ───────────────────────────────────────────────────────


def test_telemetry_event_carries_description():
    ev = TelemetryEvent.session_started(
        session_id="sess_x",
        agent_name="QA-Bot",
        agent_description="AcmeAssist QA assistant",
        options=_FakeOptions(model="m", system_prompt="..."),
        prompt="What is X?",
    )
    assert ev.agent_name == "QA-Bot"
    assert ev.agent_description == "AcmeAssist QA assistant"


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


# ── AgentRegistrySink — Mongo doc + inline manifest ──────────────────────


@pytest.fixture
def mock_client():
    return AsyncMongoMockClient()


def _ev_with_desc(payload, desc):
    return TelemetryEvent(
        kind="session_started",
        session_id="sess_d",
        agent_name="QA-Bot",
        agent_description=desc,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_registry_doc_carries_description(mock_client):
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(
        _ev_with_desc(
            {"model": "m", "system_prompt": "...", "prompt": "q"},
            "Specialist in pytest fixtures.",
        )
    )

    docs = await mock_client["agentos_test"]["agent_registry"].find().to_list(length=10)
    assert len(docs) == 1
    d = docs[0]
    assert d["_id"] == "QA-Bot"
    assert d["description"] == "Specialist in pytest fixtures."
    # And the inline manifest carries the same value, for harness clones
    # that read manifest.description directly.
    assert d["source"]["manifest"]["description"] == "Specialist in pytest fixtures."


@pytest.mark.asyncio
async def test_registry_doc_omits_manifest_description_when_unset(mock_client):
    """Backwards-compat — pre-0.1.2 callers (no description) get the same
    manifest shape they had in 0.1.1 (no `description` key)."""
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(
        TelemetryEvent(
            kind="session_started",
            session_id="sess_e",
            agent_name="QA-Bot",
            payload={"model": "m", "system_prompt": "...", "prompt": "q"},
        )
    )

    docs = await mock_client["agentos_test"]["agent_registry"].find().to_list(length=10)
    d = docs[0]
    # Top-level description is "" (not omitted — keeps the schema stable
    # so downstream SPAs don't have to special-case "missing").
    assert d["description"] == ""
    # Inline manifest: description key is absent (manifest stays minimal).
    assert "description" not in d["source"]["manifest"]


@pytest.mark.asyncio
async def test_agent_yaml_contains_description(mock_client):
    """The agent.yaml inside the inline source is what the harness clones
    into the sandbox workdir. Verify the description is rendered as a
    top-level YAML field (between name and model)."""
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(
        _ev_with_desc(
            {"model": "m", "system_prompt": "...", "prompt": "q"},
            "Pytest specialist",
        )
    )

    docs = await mock_client["agentos_test"]["agent_registry"].find().to_list(length=10)
    yaml = docs[0]["source"]["files"]["agent.yaml"]
    assert 'description: "Pytest specialist"' in yaml
    # Position: between `name` and `model` (matches the rendering order).
    name_idx = yaml.index("name:")
    desc_idx = yaml.index("description:")
    model_idx = yaml.index("model:")
    assert name_idx < desc_idx < model_idx


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
