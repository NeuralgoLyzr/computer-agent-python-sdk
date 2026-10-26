"""OTLP exports split to stay under the egress proxy's 1 MiB body limit.

Decision D13 in ``.claude/plans/p1-sdk-egress-routes.md``. Each export is
encoded with the OTLP protobuf encoders the HTTP exporters use; a batch over
:data:`OTLP_BUDGET_BYTES` is halved recursively (metrics split by metric
within their resource and scope) and each part sent separately. A single span,
log record or metric over the budget is dropped with one WARN carrying the
count only. The export fails if any part failed; the other parts are still
sent. Compression stays off: the proxy measures the body as sent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = logging.getLogger("computeragent.telemetry.otlp")

#: The largest encoded body sent, under the proxy's 1 MiB limit.
OTLP_BUDGET_BYTES = 960 * 1024

T = TypeVar("T")


def split_to_budget(
    items: Sequence[T], size_of: Callable[[Sequence[T]], int], budget: int = OTLP_BUDGET_BYTES
) -> tuple[list[list[T]], int]:
    """Parts of ``items`` (order kept) each encoding to at most ``budget``
    bytes, and the number of single items that alone exceed it."""
    if not items:
        return [], 0
    if size_of(items) <= budget:
        return [list(items)], 0
    if len(items) == 1:
        return [], 1
    mid = len(items) // 2
    left, dropped_left = split_to_budget(items[:mid], size_of, budget)
    right, dropped_right = split_to_budget(items[mid:], size_of, budget)
    return left + right, dropped_left + dropped_right


def _warn_dropped(count: int) -> None:
    if count:
        logger.warning("dropped %d telemetry record(s) over the OTLP size budget", count)


def _span_size(spans: Sequence[Any]) -> int:
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

    return int(encode_spans(spans).ByteSize())


def _log_size(records: Sequence[Any]) -> int:
    from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs

    return int(encode_logs(records).ByteSize())


class SizeCappedSpanExporter:
    """A span exporter that sends each batch in parts under the budget."""

    def __init__(self, inner: Any, *, budget: int = OTLP_BUDGET_BYTES) -> None:
        self._inner = inner
        self._budget = budget

    def export(self, spans: Sequence[Any]) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        parts, dropped = split_to_budget(spans, _span_size, self._budget)
        _warn_dropped(dropped)
        result = SpanExportResult.SUCCESS
        for part in parts:
            if self._inner.export(part) != SpanExportResult.SUCCESS:
                result = SpanExportResult.FAILURE
        return result

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return bool(self._inner.force_flush(timeout_millis))


class SizeCappedLogExporter:
    """A log record exporter that sends each batch in parts under the budget."""

    def __init__(self, inner: Any, *, budget: int = OTLP_BUDGET_BYTES) -> None:
        self._inner = inner
        self._budget = budget

    def export(self, batch: Sequence[Any]) -> Any:
        parts, dropped = split_to_budget(batch, _log_size, self._budget)
        _warn_dropped(dropped)
        failure = None
        success = None
        for part in parts:
            outcome = self._inner.export(part)
            if getattr(outcome, "name", "") == "SUCCESS":
                success = outcome
            else:
                failure = outcome
        if failure is not None:
            return failure
        if success is not None:
            return success
        from opentelemetry.sdk._logs.export import LogRecordExportResult

        return LogRecordExportResult.SUCCESS

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return bool(self._inner.force_flush(timeout_millis))


def _metric_parts(metrics_data: Any, budget: int) -> tuple[list[Any], int]:
    from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
    from opentelemetry.sdk.metrics.export import MetricsData, ResourceMetrics, ScopeMetrics

    # One entry per metric, remembering its resource and scope.
    flat = [
        (rm, sm, metric)
        for rm in metrics_data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
    ]

    def build(entries: Sequence[Any]) -> Any:
        resources: list[Any] = []
        for rm, sm, metric in entries:
            if not resources or resources[-1][0] is not rm:
                resources.append((rm, []))
            scopes = resources[-1][1]
            if not scopes or scopes[-1][0] is not sm:
                scopes.append((sm, []))
            scopes[-1][1].append(metric)
        return MetricsData(
            resource_metrics=[
                ResourceMetrics(
                    resource=rm.resource,
                    scope_metrics=[
                        ScopeMetrics(scope=sm.scope, metrics=metrics, schema_url=sm.schema_url)
                        for sm, metrics in scopes
                    ],
                    schema_url=rm.schema_url,
                )
                for rm, scopes in resources
            ]
        )

    parts, dropped = split_to_budget(
        flat, lambda entries: int(encode_metrics(build(entries)).ByteSize()), budget
    )
    return [build(part) for part in parts], dropped


def size_capped_metric_exporter(inner: Any, *, budget: int = OTLP_BUDGET_BYTES) -> Any:
    """A metric exporter that sends each collection in parts under the budget,
    keeping the inner exporter's temporality and aggregation preferences."""
    from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult

    class SizeCappedMetricExporter(MetricExporter):
        def __init__(self) -> None:
            super().__init__(
                preferred_temporality=getattr(inner, "_preferred_temporality", None),
                preferred_aggregation=getattr(inner, "_preferred_aggregation", None),
            )
            self.inner = inner
            self.budget = budget

        def export(
            self, metrics_data: Any, timeout_millis: float = 10_000, **kwargs: Any
        ) -> MetricExportResult:
            parts, dropped = _metric_parts(metrics_data, self.budget)
            _warn_dropped(dropped)
            result = MetricExportResult.SUCCESS
            for part in parts:
                outcome = self.inner.export(part, timeout_millis=timeout_millis, **kwargs)
                if outcome != MetricExportResult.SUCCESS:
                    result = MetricExportResult.FAILURE
            return result

        def force_flush(self, timeout_millis: float = 10_000) -> bool:
            return bool(self.inner.force_flush(timeout_millis=timeout_millis))

        def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
            self.inner.shutdown(timeout_millis=timeout_millis, **kwargs)

    return SizeCappedMetricExporter()


__all__ = [
    "OTLP_BUDGET_BYTES",
    "SizeCappedLogExporter",
    "SizeCappedSpanExporter",
    "size_capped_metric_exporter",
    "split_to_budget",
]
