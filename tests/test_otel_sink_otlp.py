"""Regression coverage for the four interlocking 0.1.0 OTel-sink bugs that
combined to make OTLP exports silently inert against any real backend.

Each test corresponds to one named bug in CHANGELOG `[0.1.1]`:

1. envvar fallback when `_CONFIG` is empty (the `configure(sinks=[OtelSink()])`
   construction-order trap)
2. `_signal_url` per-signal path appending (the `https://otlp.nr-data.net` → 404 trap)
3. child spans nest under invoke_agent (the fragmented-trace trap)
4. metric exporter uses delta temporality (the NR-shows-zero trap)

The existing `test_otel_sink.py` exercises the in-memory injection path and
stays green; this file specifically exercises the env-resolve and OTLP
construction paths that the in-memory tests skip past.
"""

from __future__ import annotations

import pytest

opentelemetry = pytest.importorskip("opentelemetry")
from opentelemetry.sdk.metrics.export import (  # noqa: E402
    AggregationTemporality,
    MetricExporter,
    MetricExportResult,
)
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from computeragent.telemetry import TelemetryEvent  # noqa: E402
from computeragent.telemetry.config import _reset_for_tests  # noqa: E402
from computeragent.telemetry.sinks import OtelSink  # noqa: E402
from computeragent.telemetry.sinks import otel as otel_mod  # noqa: E402


class _FakeMetricExporterBase(MetricExporter):
    """Minimal `MetricExporter` stub. `PeriodicExportingMetricReader` reaches
    into `_preferred_temporality` / `_preferred_aggregation` on the exporter
    at construction time, so we must pass through the base class properly."""

    def export(self, _metrics, _timeout_millis=10_000):
        return MetricExportResult.SUCCESS

    def shutdown(self, _timeout_millis=30_000, **_kwargs):
        return None

    def force_flush(self, _timeout_millis=10_000):
        return True


@pytest.fixture(autouse=True)
def _clean_config_state():
    """Each test starts from a fresh `_CONFIG`; the env-fallback path is what
    we're exercising and we don't want a previous `configure()` call to mask
    the bug we're regression-testing."""
    _reset_for_tests()
    yield
    _reset_for_tests()


# ── Bug 1: envvar fallback when _CONFIG is empty ────────────────────────────


def test_envvar_fallback_when_config_empty(monkeypatch):
    """Without a prior `configure()`, `OtelSink()` must still pick up the
    standard OTel env vars — otherwise users following the documented
    `configure(sinks=[OtelSink(), ...])` pattern silently export to the
    console because the sinks list is built before `_CONFIG` is populated.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.nr-data.net")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "api-key=NRTEST-12345")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "envvar-fallback-test")

    captured: dict[str, object] = {}

    class _FakeSpanExporter:
        def __init__(self, *, endpoint=None, headers=None, **_kw):
            captured["span_endpoint"] = endpoint
            captured["span_headers"] = headers

        def export(self, _spans):
            return 0

        def shutdown(self):
            return None

        def force_flush(self, _t=None):
            return True

    class _FakeMetricExporter(_FakeMetricExporterBase):
        def __init__(self, *, endpoint=None, headers=None, **kwargs):
            captured["metric_endpoint"] = endpoint
            captured["metric_headers"] = headers
            super().__init__(
                preferred_temporality=kwargs.get("preferred_temporality"),
            )

    monkeypatch.setattr(otel_mod, "HttpSpanExporter", _FakeSpanExporter)
    monkeypatch.setattr(otel_mod, "HttpMetricExporter", _FakeMetricExporter)

    sink = OtelSink()

    assert sink._endpoint == "https://otlp.nr-data.net"
    assert sink._exporter == "otlp-http"
    assert sink._headers == {"api-key": "NRTEST-12345"}
    assert sink._service_name == "envvar-fallback-test"
    # Spans go to /v1/traces, metrics to /v1/metrics — see Bug 2 test for the
    # standalone _signal_url contract; here we cover the OtelSink wiring.
    assert captured["span_endpoint"] == "https://otlp.nr-data.net/v1/traces"
    assert captured["metric_endpoint"] == "https://otlp.nr-data.net/v1/metrics"


# ── Bug 2: _signal_url per-signal path appending ─────────────────────────────


@pytest.mark.parametrize(
    ("endpoint", "signal", "expected"),
    [
        (None, "traces", None),
        ("https://otlp.nr-data.net", "traces", "https://otlp.nr-data.net/v1/traces"),
        ("https://otlp.nr-data.net", "metrics", "https://otlp.nr-data.net/v1/metrics"),
        # Trailing slash on the base URL is normalised away before appending.
        ("https://otlp.nr-data.net/", "traces", "https://otlp.nr-data.net/v1/traces"),
        # Idempotent: a caller who already provided the signal-specific URL
        # gets it back unchanged (no double-append).
        (
            "https://otlp.nr-data.net/v1/traces",
            "traces",
            "https://otlp.nr-data.net/v1/traces",
        ),
        (
            "https://otlp.nr-data.net/v1/metrics",
            "metrics",
            "https://otlp.nr-data.net/v1/metrics",
        ),
        # Self-hosted collector on a port — same rules.
        ("http://collector.internal:4318", "traces", "http://collector.internal:4318/v1/traces"),
    ],
)
def test_signal_url_appends_per_signal_path(endpoint, signal, expected):
    assert otel_mod._signal_url(endpoint, signal) == expected


# ── Bug 3: child spans nest under invoke_agent ───────────────────────────────


def _build_inmemory_otel() -> tuple[OtelSink, InMemorySpanExporter]:
    span_exp = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exp))
    sink = OtelSink(tracer_provider=tracer_provider)
    return sink, span_exp


def _ev(kind, payload=None, sid="sess_tree", agent_name="NordAssist"):
    return TelemetryEvent(kind=kind, session_id=sid, agent_name=agent_name, payload=payload or {})


@pytest.mark.asyncio
async def test_child_spans_nest_under_invoke_agent():
    sink, exp = _build_inmemory_otel()

    await sink.emit(_ev("session_started", {"model": "claude-sonnet-4-5-20250929"}))
    await sink.emit(_ev("assistant_message", {"model": "claude-sonnet-4-5-20250929"}))
    await sink.emit(_ev("tool_use", {"tool_name": "Read", "tool_use_id": "tu_1"}))
    await sink.emit(_ev("tool_result", {"tool_use_id": "tu_1", "is_error": False}))
    await sink.emit(_ev("session_ended", {"is_error": False, "duration_ms": 1234.0}))

    spans = exp.get_finished_spans()
    by_prefix = {
        prefix: [s for s in spans if s.name.startswith(prefix)]
        for prefix in ("invoke_agent", "chat", "execute_tool")
    }

    [invoke] = by_prefix["invoke_agent"]
    [chat] = by_prefix["chat"]
    [tool] = by_prefix["execute_tool"]

    # One coherent trace
    assert invoke.context.trace_id == chat.context.trace_id == tool.context.trace_id
    # Children point at invoke_agent
    assert chat.parent is not None and chat.parent.span_id == invoke.context.span_id
    assert tool.parent is not None and tool.parent.span_id == invoke.context.span_id


# ── Bug 4: metric exporter uses delta temporality ───────────────────────────


def test_metric_exporter_uses_delta_temporality_by_default(monkeypatch):
    """The default `temporality="delta"` must pass a `preferred_temporality`
    map to `HttpMetricExporter` where every SDK instrument type is mapped to
    `AggregationTemporality.DELTA`. Without this NR ingests cumulative
    payloads and renders zero across all metrics."""
    captured: dict[str, object] = {}

    class _FakeMetricExporter(_FakeMetricExporterBase):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(
                preferred_temporality=kwargs.get("preferred_temporality"),
            )

    monkeypatch.setattr(otel_mod, "HttpMetricExporter", _FakeMetricExporter)
    # Force the otlp-http branch via explicit kwargs (no env reliance here).
    OtelSink(endpoint="https://otlp.nr-data.net", exporter="otlp-http")

    pt = captured.get("preferred_temporality")
    assert pt is not None, "delta is the default — preferred_temporality must be set"
    # Every SDK instrument class must map to DELTA.
    assert set(pt.values()) == {AggregationTemporality.DELTA}
    assert len(pt) == 6


def test_metric_exporter_cumulative_opt_out(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeMetricExporter(_FakeMetricExporterBase):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(
                preferred_temporality=kwargs.get("preferred_temporality"),
            )

    monkeypatch.setattr(otel_mod, "HttpMetricExporter", _FakeMetricExporter)
    OtelSink(
        endpoint="https://otlp.nr-data.net",
        exporter="otlp-http",
        temporality="cumulative",
    )
    assert "preferred_temporality" not in captured


# ── Bugs 1 + 4 interaction: temporality env var ─────────────────────────────


def test_otel_temporality_env_var_picks_cumulative(monkeypatch):
    monkeypatch.setenv("COMPUTERAGENT_OTEL_TEMPORALITY", "cumulative")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.nr-data.net")

    captured: dict[str, object] = {}

    class _FakeMetricExporter(_FakeMetricExporterBase):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(
                preferred_temporality=kwargs.get("preferred_temporality"),
            )

    monkeypatch.setattr(otel_mod, "HttpMetricExporter", _FakeMetricExporter)
    sink = OtelSink()
    assert sink._temporality == "cumulative"
    assert "preferred_temporality" not in captured


def test_otel_temporality_env_var_garbage_falls_back_to_delta(monkeypatch):
    """A typo in the env var must not silently disable delta — fall back to
    the default rather than failing."""
    monkeypatch.setenv("COMPUTERAGENT_OTEL_TEMPORALITY", "delts")  # typo
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.nr-data.net")

    class _FakeMetricExporter(_FakeMetricExporterBase):
        def __init__(self, **kwargs):
            super().__init__(
                preferred_temporality=kwargs.get("preferred_temporality"),
            )

    monkeypatch.setattr(otel_mod, "HttpMetricExporter", _FakeMetricExporter)
    sink = OtelSink()
    assert sink._temporality == "delta"
