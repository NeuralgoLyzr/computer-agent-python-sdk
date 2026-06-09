"""``OtelSink`` — span tree, attribute keys, metrics. Verifies parity with the
TS @computeragent/observability contract (matching keys in
examples/otel-collector/genai-materialized-columns.sql)."""

from __future__ import annotations

import pytest

opentelemetry = pytest.importorskip("opentelemetry")
from opentelemetry.sdk.metrics import MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from computeragent.telemetry import TelemetryEvent  # noqa: E402
from computeragent.telemetry.sinks import OtelSink  # noqa: E402


def _build_inmemory_otel() -> tuple[OtelSink, InMemorySpanExporter, InMemoryMetricReader]:
    span_exp = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exp))

    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[metric_reader])

    sink = OtelSink(tracer_provider=tracer_provider, meter_provider=meter_provider)
    return sink, span_exp, metric_reader


def _ev(kind, payload=None, sid="sess_X", agent_name="AcmeAssist"):
    return TelemetryEvent(kind=kind, session_id=sid, agent_name=agent_name, payload=payload or {})


@pytest.mark.asyncio
async def test_invoke_agent_span_attributes():
    sink, exp, _ = _build_inmemory_otel()
    await sink.emit(
        _ev(
            "session_started",
            {
                "model": "us.anthropic.claude-sonnet-4-6",
                "engine": "claude-agent-sdk",
                "agent_version": "1.2.3",
                "agent_sha": "abc123",
            },
        )
    )
    # Usage is sourced from usage_snapshot (per-turn deltas), not session_ended.
    await sink.emit(
        _ev(
            "usage_snapshot",
            {
                "model": "us.anthropic.claude-sonnet-4-6",
                "total_cost_usd": 0.42,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 10,
                },
            },
        )
    )
    await sink.emit(_ev("session_ended", {"is_error": False, "duration_ms": 1234.0}))

    spans = exp.get_finished_spans()
    [invoke] = [s for s in spans if s.name.startswith("invoke_agent")]
    attrs = dict(invoke.attributes or {})
    # Item 1 — provider.name (not gen_ai.system) + engine name.
    assert "gen_ai.system" not in attrs
    assert attrs["gen_ai.provider.name"] == "anthropic"
    assert attrs["computeragent.engine.name"] == "claude-agent-sdk"
    assert attrs["gen_ai.agent.name"] == "AcmeAssist"
    # Item 2 — agent version + id from the manifest.
    assert attrs["gen_ai.agent.version"] == "1.2.3"
    assert attrs["gen_ai.agent.id"] == "abc123"
    assert attrs["gen_ai.conversation.id"] == "sess_X"
    assert attrs["gen_ai.operation.name"] == "invoke_agent"
    assert attrs["gen_ai.request.model"] == "us.anthropic.claude-sonnet-4-6"
    assert attrs["gen_ai.usage.input_tokens"] == 100
    assert attrs["gen_ai.usage.output_tokens"] == 50
    # Item 2 — dotted cache-token keys (match TS + the SQL contract).
    assert attrs["gen_ai.usage.cache_read.input_tokens"] == 20
    assert attrs["gen_ai.usage.cache_creation.input_tokens"] == 10
    assert "gen_ai.usage.cache_read_input_tokens" not in attrs
    assert "gen_ai.usage.cache_creation_input_tokens" not in attrs
    # cost on the root span (matches the materialized-column contract).
    assert abs(attrs["computeragent.usage.cost_usd"] - 0.42) < 1e-9


@pytest.mark.asyncio
async def test_chat_span_per_assistant_message():
    sink, exp, _ = _build_inmemory_otel()
    await sink.emit(_ev("session_started", {"model": "m"}))
    await sink.emit(
        _ev("assistant_message", {"model": "m", "text": "hi", "stop_reason": "end_turn"})
    )
    await sink.emit(_ev("session_ended", {"is_error": False, "duration_ms": 10.0}))

    chats = [s for s in exp.get_finished_spans() if s.name.startswith("chat ")]
    assert len(chats) == 1
    attrs = dict(chats[0].attributes or {})
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.request.model"] == "m"
    assert attrs["gen_ai.conversation.id"] == "sess_X"
    # Item 1 — provider on the chat span.
    assert attrs["gen_ai.provider.name"] == "anthropic"
    # Item 2 — response.model + finish_reasons.
    assert attrs["gen_ai.response.model"] == "m"
    assert tuple(attrs["gen_ai.response.finish_reasons"]) == ("stop",)
    # Agent identity propagated onto the child chat span (not just the root) so a
    # flat `WHERE gen_ai.agent.name = X` filter selects the whole trace.
    assert attrs["gen_ai.agent.name"] == "AcmeAssist"


@pytest.mark.asyncio
async def test_chat_finish_reason_inferred_tool_call():
    sink, exp, _ = _build_inmemory_otel()
    await sink.emit(_ev("session_started", {"model": "m"}))
    # No explicit stop_reason, but the message called a tool → infer tool_call.
    await sink.emit(_ev("assistant_message", {"model": "m", "text": "", "has_tool_use": True}))
    await sink.emit(_ev("session_ended", {"is_error": False, "duration_ms": 10.0}))

    chat = next(s for s in exp.get_finished_spans() if s.name.startswith("chat "))
    assert tuple(dict(chat.attributes or {})["gen_ai.response.finish_reasons"]) == ("tool_call",)


@pytest.mark.asyncio
async def test_execute_tool_span_paired_with_tool_result():
    sink, exp, _ = _build_inmemory_otel()
    await sink.emit(_ev("session_started", {"model": "m"}))
    await sink.emit(_ev("tool_use", {"tool_name": "Read", "tool_use_id": "t1", "tool_input": {}}))
    await sink.emit(_ev("tool_result", {"tool_use_id": "t1", "is_error": False}))
    await sink.emit(_ev("session_ended", {"is_error": False, "duration_ms": 10.0}))

    tool_spans = [s for s in exp.get_finished_spans() if s.name.startswith("execute_tool")]
    assert len(tool_spans) == 1
    attrs = dict(tool_spans[0].attributes or {})
    assert attrs["gen_ai.tool.name"] == "Read"
    assert attrs["gen_ai.tool.call.id"] == "t1"
    # Agent identity propagated onto the child execute_tool span.
    assert attrs["gen_ai.agent.name"] == "AcmeAssist"


@pytest.mark.asyncio
async def test_tool_call_content_is_bare_attribute(monkeypatch):
    """Item 4 — tool args/result are bare span attributes, not .role/.content."""
    from computeragent.telemetry.config import _reset_for_tests

    _reset_for_tests()
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT", "1")
    try:
        sink, exp, _ = _build_inmemory_otel()
        await sink.emit(_ev("session_started", {"model": "m"}))
        await sink.emit(
            _ev(
                "tool_use",
                {"tool_name": "Read", "tool_use_id": "t1", "tool_input": {"path": "/x"}},
            )
        )
        await sink.emit(
            _ev("tool_result", {"tool_use_id": "t1", "is_error": False, "content": "file body"})
        )
        await sink.emit(_ev("session_ended", {"is_error": False, "duration_ms": 10.0}))

        tool = next(s for s in exp.get_finished_spans() if s.name.startswith("execute_tool"))
        attrs = dict(tool.attributes or {})
        assert "path" in attrs["gen_ai.tool.call.arguments"]
        assert attrs["gen_ai.tool.call.result"] == "file body"
        assert "gen_ai.tool.call.arguments.content" not in attrs
        assert "gen_ai.tool.call.arguments.role" not in attrs
        assert "gen_ai.tool.call.result.content" not in attrs
    finally:
        _reset_for_tests()


@pytest.mark.asyncio
async def test_token_usage_histogram_has_all_dimensions():
    sink, _, reader = _build_inmemory_otel()
    await sink.emit(_ev("session_started", {"model": "m"}))
    await sink.emit(
        _ev(
            "usage_snapshot",
            {
                "model": "m",
                "total_cost_usd": 0.05,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 10,
                },
            },
        )
    )
    await sink.emit(_ev("session_ended", {"is_error": False, "duration_ms": 10.0}))

    md = reader.get_metrics_data()
    assert md is not None
    # Flatten: resource → scope → metrics
    metrics = []
    for rm in md.resource_metrics:
        for sm in rm.scope_metrics:
            metrics.extend(sm.metrics)
    by_name = {m.name: m for m in metrics}
    assert "gen_ai.client.token.usage" in by_name
    assert "computeragent.usage.cost_usd" in by_name

    token_metric = by_name["gen_ai.client.token.usage"]
    # Collect the token-type + provider dimensions across data points.
    token_types = set()
    providers = set()
    for dp in token_metric.data.data_points:
        token_types.add(dict(dp.attributes).get("gen_ai.token.type"))
        providers.add(dict(dp.attributes).get("gen_ai.provider.name"))
    assert token_types == {"input", "output", "cache_read", "cache_creation"}
    # Item 3 — provider dimension present on the histogram.
    assert providers == {"anthropic"}

    # Item 3 — cost histogram unit is "USD" (not "{usd}").
    assert by_name["computeragent.usage.cost_usd"].unit == "USD"


@pytest.mark.asyncio
async def test_cost_metric_recorded():
    sink, _, reader = _build_inmemory_otel()
    await sink.emit(_ev("session_started", {"model": "m"}))
    await sink.emit(_ev("usage_snapshot", {"model": "m", "total_cost_usd": 0.123, "usage": {}}))
    await sink.emit(_ev("session_ended", {"is_error": False, "duration_ms": 10.0}))

    md = reader.get_metrics_data()
    metrics = [m for rm in md.resource_metrics for sm in rm.scope_metrics for m in sm.metrics]
    cost = next(m for m in metrics if m.name == "computeragent.usage.cost_usd")
    sums = [dp.sum for dp in cost.data.data_points]
    assert any(abs(s - 0.123) < 1e-6 for s in sums)


@pytest.mark.asyncio
async def test_per_turn_roots_distinct_traces_and_deltas():
    """Item 5a — each turn_started opens a fresh invoke_agent root (its own
    trace); all share gen_ai.conversation.id; each root carries that turn's
    per-turn usage delta."""
    sink, exp, _ = _build_inmemory_otel()
    await sink.emit(_ev("session_started", {"model": "m"}))
    # Turn 1
    await sink.emit(_ev("turn_started", {"prompt": "first", "model": "m"}))
    await sink.emit(_ev("assistant_message", {"model": "m", "text": "a1"}))
    await sink.emit(
        _ev("usage_snapshot", {"model": "m", "usage": {"input_tokens": 100, "output_tokens": 50}})
    )
    # Turn 2
    await sink.emit(_ev("turn_started", {"prompt": "second", "model": "m"}))
    await sink.emit(_ev("assistant_message", {"model": "m", "text": "a2"}))
    await sink.emit(
        _ev("usage_snapshot", {"model": "m", "usage": {"input_tokens": 200, "output_tokens": 70}})
    )
    await sink.emit(_ev("session_ended", {"is_error": False, "duration_ms": 10.0}))

    invokes = [s for s in exp.get_finished_spans() if s.name.startswith("invoke_agent")]
    assert len(invokes) == 2
    # Distinct traces (one per turn), shared conversation id.
    assert invokes[0].context.trace_id != invokes[1].context.trace_id
    for inv in invokes:
        assert dict(inv.attributes or {})["gen_ai.conversation.id"] == "sess_X"
    # Per-turn input-token deltas (summed totals minus the per-turn baseline).
    deltas = sorted(dict(s.attributes or {})["gen_ai.usage.input_tokens"] for s in invokes)
    assert deltas == [100, 200]


@pytest.mark.asyncio
async def test_ttft_stamped_on_per_call_chat_span_and_metric_recorded():
    """response_first_token → gen_ai.response.time_to_first_chunk (seconds) on the
    per-call CHAT span (not the root) + the time_to_first_chunk histogram."""
    sink, exp, reader = _build_inmemory_otel()
    await sink.emit(_ev("session_started", {"model": "m"}))
    await sink.emit(_ev("turn_started", {"prompt": "hi", "model": "m"}))
    await sink.emit(_ev("response_first_token"))
    await sink.emit(_ev("assistant_message", {"model": "m", "text": "hello"}))
    await sink.emit(_ev("session_ended", {"is_error": False, "duration_ms": 10.0}))

    spans = exp.get_finished_spans()
    [chat] = [s for s in spans if s.name.startswith("chat")]
    ttft = dict(chat.attributes or {}).get("gen_ai.response.time_to_first_chunk")
    assert isinstance(ttft, (int, float))
    assert ttft >= 0.0
    # TTFT must NOT be on the root anymore.
    [invoke] = [s for s in spans if s.name.startswith("invoke_agent")]
    assert "gen_ai.response.time_to_first_chunk" not in dict(invoke.attributes or {})

    md = reader.get_metrics_data()
    names = [m.name for rm in md.resource_metrics for sm in rm.scope_metrics for m in sm.metrics]
    assert "gen_ai.client.operation.time_to_first_chunk" in names


@pytest.mark.asyncio
async def test_per_call_chat_spans_carry_their_own_usage_root_has_cost_only():
    """Each AssistantMessage → one chat span with its OWN token usage (incl.
    cache); the root carries cumulative cost only — so summing tokens over the
    trace's spans gives the turn total with no double-count."""
    sink, exp, _ = _build_inmemory_otel()
    await sink.emit(_ev("session_started", {"model": "m"}))
    await sink.emit(_ev("turn_started", {"prompt": "hi", "model": "m"}))
    # Call 1: emits a tool_use, then completes with its own usage.
    await sink.emit(
        _ev("tool_use", {"tool_name": "Read", "tool_use_id": "t1", "tool_input": {"p": "x"}})
    )
    await sink.emit(
        _ev(
            "assistant_message",
            {
                "model": "m",
                "text": "calling",
                "has_tool_use": True,
                "usage": {"input_tokens": 5, "output_tokens": 20, "cache_read_input_tokens": 9000},
            },
        )
    )
    await sink.emit(_ev("tool_result", {"tool_use_id": "t1", "content": "ok", "is_error": False}))
    # Call 2: the final answer with its own usage.
    await sink.emit(
        _ev(
            "assistant_message",
            {
                "model": "m",
                "text": "done",
                "usage": {"input_tokens": 7, "output_tokens": 30, "cache_read_input_tokens": 9100},
            },
        )
    )
    # Cumulative cost arrives once at the end.
    await sink.emit(
        _ev("usage_snapshot", {"model": "m", "total_cost_usd": 0.21, "usage": {}})
    )
    await sink.emit(_ev("session_ended", {"is_error": False, "duration_ms": 10.0}))

    spans = exp.get_finished_spans()
    chats = [s for s in spans if s.name.startswith("chat")]
    tools = [s for s in spans if s.name.startswith("execute_tool")]
    [invoke] = [s for s in spans if s.name.startswith("invoke_agent")]

    # One chat span per LLM call.
    assert len(chats) == 2
    # Per-call tokens on the chat spans (incl. cache), summed = turn total.
    in_sum = sum(int(dict(c.attributes or {}).get("gen_ai.usage.input_tokens", 0)) for c in chats)
    out_sum = sum(int(dict(c.attributes or {}).get("gen_ai.usage.output_tokens", 0)) for c in chats)
    cache_sum = sum(
        int(dict(c.attributes or {}).get("gen_ai.usage.cache_read.input_tokens", 0)) for c in chats
    )
    assert in_sum == 12 and out_sum == 50 and cache_sum == 18100
    # Tools are siblings of chats (children of the root), not nested under chat.
    assert tools and all(t.parent.span_id == invoke.context.span_id for t in tools)
    # Root holds cumulative COST, and NO token attributes (no double-count).
    rattrs = dict(invoke.attributes or {})
    assert abs(float(rattrs.get("computeragent.usage.cost_usd", 0)) - 0.21) < 1e-6
    assert "gen_ai.usage.input_tokens" not in rattrs
    assert "gen_ai.usage.output_tokens" not in rattrs


@pytest.mark.asyncio
async def test_token_totals_fall_back_to_root_when_no_per_call_usage():
    """Older SDKs may omit per-message usage. Then the cumulative totals stay on
    the root (from usage_snapshot) so token totals never vanish."""
    sink, exp, _ = _build_inmemory_otel()
    await sink.emit(_ev("session_started", {"model": "m"}))
    await sink.emit(_ev("turn_started", {"prompt": "hi", "model": "m"}))
    await sink.emit(_ev("assistant_message", {"model": "m", "text": "hi"}))  # no usage
    await sink.emit(
        _ev("usage_snapshot", {"model": "m", "usage": {"input_tokens": 100, "output_tokens": 40}})
    )
    await sink.emit(_ev("session_ended", {"is_error": False, "duration_ms": 10.0}))

    [invoke] = [s for s in exp.get_finished_spans() if s.name.startswith("invoke_agent")]
    rattrs = dict(invoke.attributes or {})
    assert int(rattrs.get("gen_ai.usage.input_tokens", 0)) == 100
    assert int(rattrs.get("gen_ai.usage.output_tokens", 0)) == 40


@pytest.mark.asyncio
async def test_error_session_ended_marks_span_error():
    sink, exp, _ = _build_inmemory_otel()
    await sink.emit(_ev("session_started", {"model": "m"}))
    await sink.emit(
        _ev(
            "session_ended",
            {
                "is_error": True,
                "subtype": "error",
                "error_type": "RuntimeError",
                "duration_ms": 10.0,
            },
        )
    )
    [invoke] = [s for s in exp.get_finished_spans() if s.name.startswith("invoke_agent")]
    attrs = dict(invoke.attributes or {})
    assert attrs.get("error.type") == "RuntimeError"


def test_agentos_otel_gateway_sets_endpoint_and_injects_cak(monkeypatch):
    # AGENTOS_OTEL_URL + the cak_ key → export THROUGH the AgentOS gateway with
    # an auto-injected Authorization: Bearer <cak_>; no vendor key needed.
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.trace import TracerProvider

    monkeypatch.setenv("AGENTOS_OTEL_URL", "https://host/agentos/api/otel")
    monkeypatch.setenv("COMPUTERAGENT_HARNESS_TOKEN", "cak_abc")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    sink = OtelSink(tracer_provider=TracerProvider(), meter_provider=MeterProvider())
    assert sink._endpoint == "https://host/agentos/api/otel"
    assert sink._headers.get("Authorization") == "Bearer cak_abc"


def test_no_gateway_leaves_direct_endpoint_unauthed(monkeypatch):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.trace import TracerProvider

    monkeypatch.delenv("AGENTOS_OTEL_URL", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.nr-data.net")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    sink = OtelSink(tracer_provider=TracerProvider(), meter_provider=MeterProvider())
    assert sink._endpoint == "https://otlp.nr-data.net"
    assert "Authorization" not in sink._headers
