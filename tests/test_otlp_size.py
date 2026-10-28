"""OTLP exports split to stay under the egress proxy's 1 MiB body limit."""

from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk._logs import LoggerProvider  # noqa: E402
from opentelemetry.sdk._logs.export import (  # noqa: E402
    InMemoryLogRecordExporter,
    LogRecordExportResult,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import (  # noqa: E402
    InMemoryMetricReader,
    MetricExporter,
    MetricExportResult,
)
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from computeragent.telemetry.sinks.otlp_size import (  # noqa: E402
    OTLP_BUDGET_BYTES,
    SizeCappedLogExporter,
    SizeCappedSpanExporter,
    _log_size,
    _span_size,
    size_capped_metric_exporter,
)


def make_spans(n: int, attr_bytes: int = 10):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("t")
    for i in range(n):
        with tracer.start_as_current_span(f"s{i}") as span:
            span.set_attribute("blob", "x" * attr_bytes)
    return list(exporter.get_finished_spans())


class Recorder:
    def __init__(
        self, fail_on: int | None = None, ok=SpanExportResult.SUCCESS, bad=SpanExportResult.FAILURE
    ):
        self.parts: list[list] = []
        self.fail_on = fail_on
        self.ok, self.bad = ok, bad

    def export(self, items, **kwargs):
        self.parts.append(list(items))
        return self.bad if self.fail_on == len(self.parts) - 1 else self.ok

    def shutdown(self, **kwargs):
        pass

    def force_flush(self, *args, **kwargs):
        return True


def test_small_batch_sent_whole():
    spans = make_spans(10)
    inner = Recorder()

    assert SizeCappedSpanExporter(inner).export(spans) == SpanExportResult.SUCCESS
    assert inner.parts == [spans]


def test_large_batch_split():
    spans = make_spans(400, attr_bytes=8 * 1024)
    inner = Recorder()

    assert SizeCappedSpanExporter(inner).export(spans) == SpanExportResult.SUCCESS

    assert len(inner.parts) > 1
    assert all(_span_size(part) <= OTLP_BUDGET_BYTES for part in inner.parts)
    assert [s for part in inner.parts for s in part] == spans


def test_oversized_record_dropped(caplog):
    small = make_spans(2)
    huge = make_spans(1, attr_bytes=2 * 1024 * 1024)
    inner = Recorder()

    with caplog.at_level("WARNING", logger="computeragent.telemetry.otlp"):
        SizeCappedSpanExporter(inner).export([small[0], huge[0], small[1]])

    assert [s for part in inner.parts for s in part] == small
    (record,) = caplog.records
    assert record.getMessage() == "dropped 1 telemetry record(s) over the OTLP size budget"
    assert "xxx" not in record.getMessage()


def test_failure_propagates():
    spans = make_spans(400, attr_bytes=8 * 1024)
    inner = Recorder(fail_on=0)

    assert SizeCappedSpanExporter(inner).export(spans) == SpanExportResult.FAILURE
    assert len(inner.parts) > 1  # the other parts were still sent


def make_logs(n: int, body_bytes: int):
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    logger = provider.get_logger("t")
    for _ in range(n):
        logger.emit(body="y" * body_bytes)
    return list(exporter.get_finished_logs())


def make_metrics(n: int, name_bytes: int):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("t")
    for i in range(n):
        meter.create_counter(f"c{i}_" + "z" * name_bytes).add(1)
    return reader.get_metrics_data()


def test_logs_and_metrics_split():
    logs = make_logs(300, body_bytes=8 * 1024)
    inner = Recorder(ok=LogRecordExportResult.SUCCESS, bad=LogRecordExportResult.FAILURE)
    assert SizeCappedLogExporter(inner).export(logs) == LogRecordExportResult.SUCCESS
    assert len(inner.parts) > 1
    assert all(_log_size(part) <= OTLP_BUDGET_BYTES for part in inner.parts)
    assert [r for part in inner.parts for r in part] == logs

    from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics

    data = make_metrics(10, name_bytes=200)
    sent: list = []

    class Inner(MetricExporter):
        def export(self, metrics_data, timeout_millis=10_000, **kwargs):
            sent.append(metrics_data)
            return MetricExportResult.SUCCESS

        def force_flush(self, timeout_millis=10_000):
            return True

        def shutdown(self, timeout_millis=30_000, **kwargs):
            pass

    budget = encode_metrics(data).ByteSize() // 3
    capped = size_capped_metric_exporter(Inner(), budget=budget)
    assert capped.export(data) == MetricExportResult.SUCCESS
    assert len(sent) > 1
    assert all(encode_metrics(part).ByteSize() <= budget for part in sent)
    names = [
        m.name
        for part in sent
        for rm in part.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    ]
    assert names == [
        m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics
    ]


def test_encoders_available():
    """Pins the encoder modules (one is private-named) by encoding each signal."""
    assert _span_size(make_spans(1)) > 0
    assert _log_size(make_logs(1, 10)) > 0
    from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics

    assert encode_metrics(make_metrics(1, 1)).ByteSize() > 0
