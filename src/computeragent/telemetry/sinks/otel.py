"""``OtelSink`` — emit GenAI-semconv-compliant OpenTelemetry spans + metrics.

Span tree matches the TS ``@computeragent/observability`` package so polyglot
deployments produce one coherent trace tree in the same backend. ONE trace per
TURN: each turn opens a fresh ``invoke_agent`` ROOT span (its own trace); all
turns of one session share ``gen_ai.conversation.id = session_id``::

    invoke_agent <agent_name>   [gen_ai.conversation.id = session_id,
                                 gen_ai.provider.name = "anthropic",
                                 computeragent.engine.name = "claude-agent-sdk",
                                 gen_ai.agent.name/version/id = ...]
    └── chat <model>            (SpanKind.CLIENT) [gen_ai.request.model,
                                 gen_ai.response.model, response.finish_reasons]
        └── execute_tool <name> (SpanKind.INTERNAL) [gen_ai.tool.name,
                                 gen_ai.tool.call.id]

A ``turn_started`` event closes the prior turn's tree (tools -> chat -> root,
children before parents) — stamping that turn's per-turn usage *delta* on the
root — then opens the next turn's root. ``session_ended`` closes the final turn.
When no ``turn_started`` precedes a child (the proxy ``query()`` 1:1 path and
unit tests), the root/chat open lazily on the first child.

Metrics::

    gen_ai.client.token.usage           (histogram, unit "{token}")
        dims: gen_ai.operation.name, gen_ai.provider.name,
              gen_ai.request.model, gen_ai.token.type
    gen_ai.client.operation.duration    (histogram, unit "s")
    computeragent.usage.cost_usd        (histogram, unit "USD") — custom
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

try:
    from opentelemetry import metrics, trace  # noqa: F401 - probe imports only
    from opentelemetry.context import Context
    from opentelemetry.exporter.otlp.proto.http._log_exporter import (
        OTLPLogExporter as HttpLogExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter as HttpMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter as HttpSpanExporter,
    )
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (
        BatchLogRecordProcessor,
        ConsoleLogExporter,
        SimpleLogRecordProcessor,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics._internal.instrument import (
        Counter as _SDKCounter,
    )
    from opentelemetry.sdk.metrics._internal.instrument import (
        Histogram as _SDKHistogram,
    )
    from opentelemetry.sdk.metrics._internal.instrument import (
        ObservableCounter as _SDKObservableCounter,
    )
    from opentelemetry.sdk.metrics._internal.instrument import (
        ObservableGauge as _SDKObservableGauge,
    )
    from opentelemetry.sdk.metrics._internal.instrument import (
        ObservableUpDownCounter as _SDKObservableUpDownCounter,
    )
    from opentelemetry.sdk.metrics._internal.instrument import (
        UpDownCounter as _SDKUpDownCounter,
    )
    from opentelemetry.sdk.metrics.export import (
        AggregationTemporality,
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.metrics.view import (
        ExplicitBucketHistogramAggregation,
        View,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )
    from opentelemetry.trace import SpanKind, Status, StatusCode
except ImportError as _exc:  # pragma: no cover - guarded by lazy export
    raise ImportError(
        "OtelSink requires the [otel] extra. Install with: pip install 'computer-agent-py[otel]'"
    ) from _exc

from .content import (
    ContentAccumulator,
    map_finish_reason,
    write_inference_content,
)

Temporality = Literal["delta", "cumulative"]

if TYPE_CHECKING:

    from ..event import TelemetryEvent

logger = logging.getLogger("computeragent.telemetry")


# GenAI semconv attribute keys — pinned literals to guarantee parity with the
# TS @computeragent/observability emission. The semconv Python package's
# constants for these keys move between alpha versions; literals are safer.
# These mirror packages/observability/src/semantic/attributes.ts one-for-one;
# the ClickHouse contract lives in examples/otel-collector/genai-materialized-columns.sql.
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_AGENT_VERSION = "gen_ai.agent.version"
GEN_AI_AGENT_ID = "gen_ai.agent.id"
GEN_AI_AGENT_DESCRIPTION = "gen_ai.agent.description"
GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GEN_AI_TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"
GEN_AI_TOOL_CALL_RESULT = "gen_ai.tool.call.result"
GEN_AI_TOKEN_TYPE = "gen_ai.token.type"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
# Dotted form per OTel GenAI semconv (matches the TS keys + the SQL contract).
GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS = "gen_ai.usage.cache_read.input_tokens"
GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS = "gen_ai.usage.cache_creation.input_tokens"
ERROR_TYPE = "error.type"

# ComputerAgent-specific extensions (computeragent.* prefix — never gen_ai.*).
COMPUTERAGENT_ENGINE_NAME = "computeragent.engine.name"
COMPUTERAGENT_USAGE_COST_USD = "computeragent.usage.cost_usd"

METRIC_TOKEN_USAGE = "gen_ai.client.token.usage"
METRIC_OPERATION_DURATION = "gen_ai.client.operation.duration"
METRIC_COST_USD = "computeragent.usage.cost_usd"

# Tracer + meter + logger instrumentation scope. Matches the TS
# INSTRUMENTATION_SCOPE_NAME so spans/metrics/logs filter identically by scope.
INSTRUMENTATION_SCOPE_NAME = "@computeragent/observability"

# Cap on retained per-session identity (session_meta / session_model). These
# maps are NOT cleared on session_ended (the SDK fires that per ComputerAgent
# lifecycle, and turns close on the next turn_started), so this bounds growth in
# a long-lived process. FIFO-evicted in ``_remember``. Mirrors TS.
MAX_TRACKED_SESSIONS = 10_000

# Spec-exact histogram bucket boundaries — verbatim from
# packages/observability/src/semantic/attributes.ts. The OTel SDK's default
# exponential histogram does not satisfy backends that expect explicit buckets.
TOKEN_USAGE_BUCKETS: tuple[int, ...] = (
    1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864,
)
OPERATION_DURATION_BUCKETS: tuple[float, ...] = (
    0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92,
)
# cost is not in the OTel spec; buckets tuned for typical per-call LLM cost.
COST_USD_BUCKETS: tuple[float, ...] = (
    0.0001, 0.001, 0.01, 0.05, 0.1, 0.5, 1, 5, 10, 50, 100,
)


def _provider_for_engine(engine_name: str) -> str:
    """Map a ComputerAgent engine name to the GenAI provider name.

    Mirrors ``providerForEngine`` in the TS OtelAuditSink exactly — both
    reference engines (``claude-agent-sdk``, ``gitagent``) report ``anthropic``;
    anything else passes through as a free-form provider name.
    """
    if engine_name in ("claude-agent-sdk", "gitagent"):
        return "anthropic"
    return engine_name


# ── span-map (port of span-map.ts) -------------------------------------------


@dataclass
class _EndOptions:
    status: str = "ok"  # "ok" | "error"
    error_type: str | None = None
    error_message: str | None = None
    attributes: dict[str, Any] | None = None


def _finalize(span: Any, opts: _EndOptions) -> None:
    """Set final attrs + OK/ERROR status, then end the span. Never raises."""
    try:
        if opts.attributes:
            span.set_attributes(opts.attributes)
        if opts.status == "error":
            if opts.error_type:
                span.set_attribute(ERROR_TYPE, opts.error_type)
            span.set_status(Status(StatusCode.ERROR, opts.error_message))
        else:
            span.set_status(Status(StatusCode.OK))
        span.end()
    except Exception:  # noqa: BLE001
        logger.debug("span finalize failed", exc_info=True)


@dataclass
class _ToolRec:
    span: Any
    tool_name: str
    opened_at: float


@dataclass
class _SessionEntry:
    invoke_agent: Any | None = None
    chat: Any | None = None
    tools: dict[str, _ToolRec] = field(default_factory=dict)


class _SpanMap:
    """Registry of open invoke_agent / chat / execute_tool spans per session.

    Port of span-map.ts: at most one invoke_agent + one chat open per session,
    any number of tools keyed by call_id. Close order is children-before-parents.
    """

    def __init__(self) -> None:
        self._per: dict[str, _SessionEntry] = {}

    def _get(self, sid: str) -> _SessionEntry:
        e = self._per.get(sid)
        if e is None:
            e = _SessionEntry()
            self._per[sid] = e
        return e

    # invoke_agent ------------------------------------------------------------

    def set_invoke_agent(self, sid: str, span: Any) -> None:
        self._get(sid).invoke_agent = span

    def get_invoke_agent(self, sid: str) -> Any | None:
        e = self._per.get(sid)
        return e.invoke_agent if e else None

    def end_invoke_agent(self, sid: str, opts: _EndOptions) -> None:
        e = self._per.get(sid)
        if e and e.invoke_agent is not None:
            _finalize(e.invoke_agent, opts)
            e.invoke_agent = None

    # chat --------------------------------------------------------------------

    def open_chat(self, sid: str, span: Any) -> None:
        e = self._get(sid)
        if e.chat is not None:  # defensive: close a leaked prior chat
            _finalize(e.chat, _EndOptions())
        e.chat = span

    def get_current_chat(self, sid: str) -> Any | None:
        e = self._per.get(sid)
        return e.chat if e else None

    def end_chat(self, sid: str, opts: _EndOptions) -> None:
        e = self._per.get(sid)
        if e and e.chat is not None:
            _finalize(e.chat, opts)
            e.chat = None

    # execute_tool ------------------------------------------------------------

    def open_tool(self, sid: str, call_id: str, tool_name: str, span: Any) -> None:
        self._get(sid).tools[call_id] = _ToolRec(span, tool_name, time.monotonic())

    def get_tool(self, sid: str, call_id: str) -> Any | None:
        e = self._per.get(sid)
        rec = e.tools.get(call_id) if e else None
        return rec.span if rec else None

    def open_tool_recs(self, sid: str) -> list[_ToolRec]:
        e = self._per.get(sid)
        return list(e.tools.values()) if e else []

    def end_tool(
        self, sid: str, call_id: str, opts: _EndOptions, tool_name: str | None = None
    ) -> bool:
        e = self._per.get(sid)
        if e is None:
            return False
        direct = e.tools.get(call_id)
        if direct is not None:
            _finalize(direct.span, opts)
            del e.tools[call_id]
            return True
        if not tool_name:
            return False
        # Fallback: oldest open tool with a matching name (gitagent-style ids).
        oldest_key: str | None = None
        oldest_at = float("inf")
        for k, rec in e.tools.items():
            if rec.tool_name == tool_name and rec.opened_at < oldest_at:
                oldest_at = rec.opened_at
                oldest_key = k
        if oldest_key is None:
            return False
        _finalize(e.tools[oldest_key].span, opts)
        del e.tools[oldest_key]
        return True

    # bulk close --------------------------------------------------------------

    def end_chat_and_tools(self, sid: str, opts: _EndOptions) -> None:
        """Close tools then chat (children before parent); leave root open."""
        e = self._per.get(sid)
        if e is None:
            return
        for rec in e.tools.values():
            _finalize(rec.span, opts)
        e.tools.clear()
        if e.chat is not None:
            _finalize(e.chat, opts)
            e.chat = None

    def end_all_for_session(self, sid: str, opts: _EndOptions) -> None:
        """Close tools -> chat -> root, then drop the session entry."""
        e = self._per.get(sid)
        if e is None:
            return
        for rec in e.tools.values():
            _finalize(rec.span, opts)
        e.tools.clear()
        if e.chat is not None:
            _finalize(e.chat, opts)
            e.chat = None
        if e.invoke_agent is not None:
            _finalize(e.invoke_agent, opts)
            e.invoke_agent = None
        self._per.pop(sid, None)


# ── usage aggregator (port of usage-aggregator.ts) ---------------------------


@dataclass
class _Totals:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cost_usd: float | None = None
    cost_semantic: str | None = None
    started_at: float = 0.0
    turn_baseline: dict[str, float] | None = None


class _UsageAggregator:
    """Accumulate usage snapshots into per-session totals + per-turn deltas.

    Owns the token / duration / cost histograms. Token histogram is recorded
    per snapshot (spec = per LLM call); duration + cost histograms once per
    session end. Cost: cumulative -> keep max, delta -> sum (default cumulative,
    never double-count). Mirrors usage-aggregator.ts.
    """

    def __init__(self, meter: Any) -> None:
        self._token_hist = meter.create_histogram(
            METRIC_TOKEN_USAGE, unit="{token}", description="GenAI token usage"
        )
        self._duration_hist = meter.create_histogram(
            METRIC_OPERATION_DURATION, unit="s", description="GenAI operation duration"
        )
        self._cost_hist = meter.create_histogram(
            METRIC_COST_USD, unit="USD", description="Inference cost in USD"
        )
        self._per: dict[str, _Totals] = {}

    def _get(self, sid: str) -> _Totals:
        t = self._per.get(sid)
        if t is None:
            t = _Totals(started_at=time.monotonic())
            self._per[sid] = t
        return t

    def ingest(self, sid: str, snap: dict[str, Any], ctx: dict[str, Any]) -> None:
        t = self._get(sid)
        semantic = snap.get("cost_semantic") or t.cost_semantic or "cumulative"
        t.cost_semantic = semantic

        dims = self._metric_attrs(ctx)
        for field_name, token_type in (
            ("input_tokens", "input"),
            ("output_tokens", "output"),
            ("cache_read_input_tokens", "cache_read"),
            ("cache_creation_input_tokens", "cache_creation"),
        ):
            v = snap.get(field_name)
            if v is None:
                continue
            prev = getattr(t, field_name) or 0
            setattr(t, field_name, prev + v)
            self._token_hist.record(v, attributes={**dims, GEN_AI_TOKEN_TYPE: token_type})

        cost = snap.get("cost_usd")
        if cost is not None:
            if semantic == "cumulative":
                t.cost_usd = max(t.cost_usd or 0.0, cost)
            else:
                t.cost_usd = (t.cost_usd or 0.0) + cost

    def mark_turn_boundary(self, sid: str) -> None:
        t = self._get(sid)
        t.turn_baseline = {
            "input_tokens": t.input_tokens or 0,
            "output_tokens": t.output_tokens or 0,
            "cache_creation_input_tokens": t.cache_creation_input_tokens or 0,
            "cache_read_input_tokens": t.cache_read_input_tokens or 0,
            "cost_usd": t.cost_usd or 0.0,
        }

    def turn_span_attributes(self, sid: str) -> dict[str, Any]:
        t = self._per.get(sid)
        if t is None:
            return {}
        base = t.turn_baseline or {}
        out: dict[str, Any] = {}
        if t.input_tokens is not None:
            out[GEN_AI_USAGE_INPUT_TOKENS] = t.input_tokens - base.get("input_tokens", 0)
        if t.output_tokens is not None:
            out[GEN_AI_USAGE_OUTPUT_TOKENS] = t.output_tokens - base.get("output_tokens", 0)
        if t.cache_creation_input_tokens is not None:
            out[GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS] = (
                t.cache_creation_input_tokens - base.get("cache_creation_input_tokens", 0)
            )
        if t.cache_read_input_tokens is not None:
            out[GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS] = (
                t.cache_read_input_tokens - base.get("cache_read_input_tokens", 0)
            )
        if t.cost_usd is not None:
            out[COMPUTERAGENT_USAGE_COST_USD] = t.cost_usd - base.get("cost_usd", 0.0)
        return out

    def end_session(
        self,
        sid: str,
        ctx: dict[str, Any],
        error_type: str | None = None,
        duration_s: float | None = None,
    ) -> None:
        t = self._per.get(sid)
        if t is None:
            return
        dims = self._metric_attrs(ctx)
        if error_type:
            dims[ERROR_TYPE] = error_type
        dur = duration_s if duration_s is not None else max(0.0, time.monotonic() - t.started_at)
        if dur > 0:
            self._duration_hist.record(dur, attributes=dims)
        if t.cost_usd is not None:
            self._cost_hist.record(t.cost_usd, attributes=dims)
        self._per.pop(sid, None)

    @staticmethod
    def _metric_attrs(ctx: dict[str, Any]) -> dict[str, Any]:
        attrs: dict[str, Any] = {GEN_AI_OPERATION_NAME: ctx.get("operation_name", "chat")}
        if ctx.get("provider"):
            attrs[GEN_AI_PROVIDER_NAME] = ctx["provider"]
        if ctx.get("request_model"):
            attrs[GEN_AI_REQUEST_MODEL] = str(ctx["request_model"])
        return attrs


@dataclass
class _SessionMeta:
    engine_name: str
    provider: str
    agent_name: str | None
    agent_description: str | None
    agent_version: str | None
    agent_sha: str | None


def _normalize_usage(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the two usage_snapshot shapes into one snapshot dict.

    Harness emits flat top-level token keys; the proxy nests them under
    ``usage``. Cost is always top-level ``total_cost_usd`` in both.
    """
    nested = payload.get("usage")
    src = nested if isinstance(nested, dict) and nested else payload

    def _int(key: str) -> int | None:
        v = src.get(key)
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    cost = payload.get("total_cost_usd")
    cost_f: float | None
    try:
        cost_f = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost_f = None

    return {
        "input_tokens": _int("input_tokens"),
        "output_tokens": _int("output_tokens"),
        "cache_creation_input_tokens": _int("cache_creation_input_tokens"),
        "cache_read_input_tokens": _int("cache_read_input_tokens"),
        "cost_usd": cost_f,
        "cost_semantic": payload.get("cost_semantic"),
    }


class OtelSink:
    """Translate :class:`TelemetryEvent` into OTel spans + metric measurements.

    Configures its own ``TracerProvider`` and ``MeterProvider`` (and, when
    content events are enabled, a ``LoggerProvider``) on first use unless one is
    passed in (tests use this hook to attach in-memory exporters).
    """

    def __init__(
        self,
        *,
        service_name: str | None = None,
        exporter: str | None = None,
        endpoint: str | None = None,
        headers: dict[str, str] | None = None,
        temporality: Temporality | None = None,
        tracer_provider: Any | None = None,
        meter_provider: Any | None = None,
        logger_provider: Any | None = None,
    ) -> None:
        # Resolve effective config. The user pattern is
        # ``configure(sinks=[OtelSink(), ...])``: Python builds the sinks list
        # (running this __init__) BEFORE configure() populates _CONFIG, so
        # falling back through cfg alone misses env vars. Resolution order:
        # explicit kwarg → configure()'d _CONFIG → env var → final default.
        from ..config import get_config

        cfg = get_config()
        self._service_name = (
            service_name
            or cfg.get("service_name")
            or os.environ.get("OTEL_SERVICE_NAME")
            or "computeragent"
        )
        self._endpoint = (
            endpoint or cfg.get("endpoint") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        )
        if headers is not None:
            self._headers: dict[str, str] = headers
        elif cfg.get("headers"):
            self._headers = cfg["headers"]
        else:
            self._headers = _parse_otlp_headers_env(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS"))
        self._exporter = exporter or cfg.get("exporter") or _infer_exporter(self._endpoint)
        self._temporality: Temporality = _resolve_temporality(temporality or cfg.get("temporality"))

        # Content capture — opt-in. When on, the sink writes the structured
        # gen_ai.input/output.messages + system_instructions. Middleware
        # (PiiRedactor, GuardrailFilter) has already mutated the payload by this
        # point, so what lands is the redacted projection — never raw content.
        self._capture_content = bool(
            cfg.get("capture_content")
            if "capture_content" in cfg
            else _env_bool("COMPUTERAGENT_CAPTURE_CONTENT", False)
        )
        mode = cfg.get("capture_content_mode") or os.environ.get(
            "COMPUTERAGENT_CAPTURE_CONTENT_MODE", "events"
        )
        if mode not in ("attributes", "events", "both"):
            mode = "events"
        self._capture_mode: str = mode
        # Cap attribute size so a runaway prompt doesn't blow OTLP packet
        # limits. Default 4096 to match the TS maxAttributeLength.
        try:
            self._max_attr_length = int(
                cfg.get("max_attribute_length")
                or os.environ.get("COMPUTERAGENT_MAX_ATTRIBUTE_LENGTH", "4096")
            )
        except (TypeError, ValueError):
            self._max_attr_length = 4096

        self._tracer_provider = tracer_provider or _build_tracer_provider(
            service_name=self._service_name,
            exporter=self._exporter,
            endpoint=self._endpoint,
            headers=self._headers,
        )
        self._meter_provider = meter_provider or _build_meter_provider(
            service_name=self._service_name,
            exporter=self._exporter,
            endpoint=self._endpoint,
            headers=self._headers,
            temporality=self._temporality,
        )
        self._tracer = self._tracer_provider.get_tracer(INSTRUMENTATION_SCOPE_NAME)
        self._meter = self._meter_provider.get_meter(INSTRUMENTATION_SCOPE_NAME)

        # Logger provider only when content events are needed (mirrors the TS
        # needsLoggerProvider gate). Attributes-only mode boots no logs pipeline.
        self._logger_provider: Any | None = None
        self._logger: Any | None = None
        if logger_provider is not None:
            self._logger_provider = logger_provider
            self._logger = logger_provider.get_logger(INSTRUMENTATION_SCOPE_NAME)
        elif self._capture_content and self._capture_mode in ("events", "both"):
            self._logger_provider = _build_logger_provider(
                service_name=self._service_name,
                exporter=self._exporter,
                endpoint=self._endpoint,
                headers=self._headers,
            )
            self._logger = self._logger_provider.get_logger(INSTRUMENTATION_SCOPE_NAME)

        self._spans = _SpanMap()
        self._usage = _UsageAggregator(self._meter)
        self._content = ContentAccumulator()
        # Per-session identity, retained across turns (NOT cleared on
        # session_ended) and FIFO-bounded via _remember.
        self._session_meta: dict[str, _SessionMeta] = {}
        self._session_model: dict[str, str] = {}

    # ── lifecycle ----------------------------------------------------------

    async def emit(self, event: TelemetryEvent) -> None:
        handler = _DISPATCH.get(event.kind)
        if handler is None:
            return
        handler(self, event)

    async def flush(self, timeout: float = 5.0) -> None:
        ms = int(timeout * 1000)
        for name, provider in (
            ("tracer_provider", self._tracer_provider),
            ("meter_provider", self._meter_provider),
            ("logger_provider", self._logger_provider),
        ):
            if provider is None:
                continue
            try:
                provider.force_flush(timeout_millis=ms)
            except Exception:  # noqa: BLE001
                logger.debug("%s.force_flush failed", name, exc_info=True)

    async def aclose(self) -> None:
        await self.flush()
        for name, provider in (
            ("tracer_provider", self._tracer_provider),
            ("meter_provider", self._meter_provider),
            ("logger_provider", self._logger_provider),
        ):
            if provider is None:
                continue
            try:
                provider.shutdown()
            except Exception:  # noqa: BLE001
                logger.debug("%s.shutdown failed", name, exc_info=True)

    # ── small helpers -------------------------------------------------------

    @staticmethod
    def _remember(map_: dict[str, Any], key: str, value: Any) -> None:
        """Insert with FIFO eviction so retained identity can't grow unbounded."""
        if key not in map_ and len(map_) >= MAX_TRACKED_SESSIONS:
            oldest = next(iter(map_), None)
            if oldest is not None:
                map_.pop(oldest, None)
        map_[key] = value

    def _provider(self, sid: str) -> str | None:
        meta = self._session_meta.get(sid)
        return meta.provider if meta else None

    def _metric_ctx(self, sid: str) -> dict[str, Any]:
        return {
            "operation_name": "chat",
            "provider": self._provider(sid),
            "request_model": self._session_model.get(sid),
        }

    def _event_attrs(self, sid: str) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            GEN_AI_OPERATION_NAME: "chat",
            GEN_AI_CONVERSATION_ID: sid,
        }
        provider = self._provider(sid)
        if provider:
            attrs[GEN_AI_PROVIDER_NAME] = provider
        model = self._session_model.get(sid)
        if model:
            attrs[GEN_AI_REQUEST_MODEL] = model
        return attrs

    def _write_tool_content(self, span: Any, key: str, serialised: str) -> None:
        """Write tool-call arguments/result as a BARE span attribute.

        Tool-call content is span-attribute-only per the GenAI semconv (no
        event, no ``.role``/``.content`` sub-keys) — matches TS
        ``writeToolCallContent``. Gated on ``capture_content`` and truncated.
        """
        if not self._capture_content or not serialised:
            return
        text = (
            serialised
            if len(serialised) <= self._max_attr_length
            else (serialised[: self._max_attr_length - 14] + "...[truncated]")
        )
        span.set_attribute(key, text)

    def _flush_turn_content(self, sid: str) -> None:
        """Write the accumulated turn content onto the chat span (attributes)
        and/or one inference-details log record (events). No-op when capture
        is off."""
        if not self._capture_content:
            return
        target = self._spans.get_current_chat(sid) or self._spans.get_invoke_agent(sid)
        write_inference_content(
            input_msgs=self._content.input_for(sid),
            output_msgs=self._content.output_for(sid),
            system_parts=self._content.system_for(sid),
            capture_content=self._capture_content,
            capture_mode=self._capture_mode,
            max_attr_length=self._max_attr_length,
            span=target,
            logger_=self._logger,
            event_attributes=self._event_attrs(sid),
        )

    # ── span open helpers ---------------------------------------------------

    def _open_invoke_agent(self, sid: str) -> None:
        if self._spans.get_invoke_agent(sid) is not None:
            return
        meta = self._session_meta.get(sid)
        if meta is None:
            return
        attrs: dict[str, Any] = {
            GEN_AI_OPERATION_NAME: "invoke_agent",
            GEN_AI_PROVIDER_NAME: meta.provider,
            COMPUTERAGENT_ENGINE_NAME: meta.engine_name,
            GEN_AI_CONVERSATION_ID: sid,
        }
        if meta.agent_name:
            attrs[GEN_AI_AGENT_NAME] = meta.agent_name
        if meta.agent_version:
            attrs[GEN_AI_AGENT_VERSION] = meta.agent_version
        if meta.agent_sha:
            attrs[GEN_AI_AGENT_ID] = meta.agent_sha
        if meta.agent_description:
            attrs[GEN_AI_AGENT_DESCRIPTION] = meta.agent_description
        model = self._session_model.get(sid)
        if model:
            attrs[GEN_AI_REQUEST_MODEL] = model
        # Pin to an empty Context so each turn root is its own trace and never
        # nests under an ambient active span.
        span = self._tracer.start_span(
            name=f"invoke_agent {meta.agent_name or 'agent'}",
            kind=SpanKind.INTERNAL,
            attributes=attrs,
            context=Context(),
        )
        self._spans.set_invoke_agent(sid, span)

    def _ensure_invoke_agent(self, sid: str) -> None:
        """Lazily open the turn root (marking the usage baseline) when no
        ``turn_started`` preceded the first child — the proxy ``query()`` 1:1
        path and the unit tests."""
        if self._spans.get_invoke_agent(sid) is not None:
            return
        if sid not in self._session_meta:
            return
        self._usage.mark_turn_boundary(sid)
        self._open_invoke_agent(sid)

    def _ensure_chat_span(self, sid: str) -> None:
        if self._spans.get_current_chat(sid) is not None:
            return
        self._ensure_invoke_agent(sid)
        root = self._spans.get_invoke_agent(sid)
        model = self._session_model.get(sid)
        attrs: dict[str, Any] = {
            GEN_AI_OPERATION_NAME: "chat",
            GEN_AI_CONVERSATION_ID: sid,
        }
        provider = self._provider(sid)
        if provider:
            attrs[GEN_AI_PROVIDER_NAME] = provider
        if model:
            attrs[GEN_AI_REQUEST_MODEL] = model
        ctx = trace.set_span_in_context(root) if root is not None else None
        span = self._tracer.start_span(
            name=f"chat {model}" if model else "chat",
            kind=SpanKind.CLIENT,
            attributes=attrs,
            context=ctx,
        )
        self._spans.open_chat(sid, span)

    # ── handlers (one per event kind) --------------------------------------

    def _on_session_started(self, event: TelemetryEvent) -> None:
        sid = event.session_id
        engine_name = str(event.payload.get("engine") or "claude-agent-sdk")
        agent_version = event.payload.get("agent_version")
        agent_sha = event.payload.get("agent_sha")
        meta = _SessionMeta(
            engine_name=engine_name,
            provider=_provider_for_engine(engine_name),
            agent_name=event.agent_name,
            agent_description=event.agent_description,
            agent_version=str(agent_version) if agent_version else None,
            agent_sha=str(agent_sha) if agent_sha else None,
        )
        self._remember(self._session_meta, sid, meta)
        model = event.payload.get("model")
        if model:
            self._remember(self._session_model, sid, str(model))
        # System instructions captured once; surfaces on the first turn's flush.
        # The user prompt is captured by turn_started (TS-faithful), not here.
        system_prompt = event.payload.get("system_prompt")
        if self._capture_content and isinstance(system_prompt, str) and system_prompt:
            self._content.record_system_instructions(sid, system_prompt)
        # No span opens here — the per-turn root opens on turn_started (or
        # lazily on the first child).

    def _on_turn_started(self, event: TelemetryEvent) -> None:
        sid = event.session_id
        # Close the prior turn's tree, stamping its usage delta on the root.
        if self._spans.get_invoke_agent(sid) is not None:
            self._flush_turn_content(sid)
            root = self._spans.get_invoke_agent(sid)
            if root is not None:
                root.set_attributes(self._usage.turn_span_attributes(sid))
            self._spans.end_chat_and_tools(sid, _EndOptions(status="ok"))
            self._spans.end_invoke_agent(sid, _EndOptions(status="ok"))
        # Each turn is a distinct operation with its own content set.
        self._content.reset(sid)
        model = event.payload.get("model")
        if model:
            self._remember(self._session_model, sid, str(model))
        # Open this turn's root (marks the per-turn usage baseline).
        self._ensure_invoke_agent(sid)
        prompt = event.payload.get("prompt")
        if (
            self._capture_content
            and isinstance(prompt, str)
            and prompt
            and prompt != "<streaming>"
        ):
            self._content.record_user_text(sid, prompt)

    def _on_assistant_message(self, event: TelemetryEvent) -> None:
        sid = event.session_id
        model = event.payload.get("model")
        if model:
            self._remember(self._session_model, sid, str(model))
        self._ensure_chat_span(sid)
        chat = self._spans.get_current_chat(sid)
        if chat is not None:
            if model:
                chat.set_attribute(GEN_AI_REQUEST_MODEL, str(model))
                chat.set_attribute(GEN_AI_RESPONSE_MODEL, str(model))
            finish_reason = map_finish_reason(
                event.payload.get("stop_reason"),
                bool(event.payload.get("has_tool_use")),
            )
            chat.set_attribute(GEN_AI_RESPONSE_FINISH_REASONS, [finish_reason])
        text = event.payload.get("text")
        if self._capture_content and isinstance(text, str) and text:
            self._content.record_assistant_text(
                sid,
                text,
                stop_reason=event.payload.get("stop_reason"),
                has_tool_use=bool(event.payload.get("has_tool_use")),
            )

    def _on_tool_use(self, event: TelemetryEvent) -> None:
        sid = event.session_id
        tool_name = str(event.payload.get("tool_name") or "tool")
        call_id = str(event.payload.get("tool_use_id") or "")
        self._ensure_chat_span(sid)
        if call_id and self._spans.get_tool(sid, call_id) is not None:
            return  # idempotent — don't double-open
        parent = self._spans.get_current_chat(sid) or self._spans.get_invoke_agent(sid)
        attrs: dict[str, Any] = {
            GEN_AI_OPERATION_NAME: "execute_tool",
            GEN_AI_TOOL_NAME: tool_name,
            GEN_AI_CONVERSATION_ID: sid,
        }
        if call_id:
            attrs[GEN_AI_TOOL_CALL_ID] = call_id
        ctx = trace.set_span_in_context(parent) if parent is not None else None
        span = self._tracer.start_span(
            name=f"execute_tool {tool_name}",
            kind=SpanKind.INTERNAL,
            attributes=attrs,
            context=ctx,
        )
        self._spans.open_tool(sid, call_id, tool_name, span)
        # Tool arguments — JSON-encoded so multi-key inputs survive the
        # attribute-string projection.
        tool_input = event.payload.get("tool_input")
        if isinstance(tool_input, dict) and tool_input:
            try:
                serialised = json.dumps(tool_input, default=str)
            except (TypeError, ValueError):
                serialised = str(tool_input)
            self._write_tool_content(span, GEN_AI_TOOL_CALL_ARGUMENTS, serialised)
            if self._capture_content:
                self._content.record_tool_args(call_id, tool_input)

    def _on_tool_result(self, event: TelemetryEvent) -> None:
        sid = event.session_id
        call_id = str(event.payload.get("tool_use_id") or "")
        is_error = bool(event.payload.get("is_error"))
        span = self._spans.get_tool(sid, call_id)
        content = event.payload.get("content")
        if span is not None and content:
            if isinstance(content, str):
                serialised = content
            else:
                try:
                    serialised = json.dumps(content, default=str)
                except (TypeError, ValueError):
                    serialised = str(content)
            self._write_tool_content(span, GEN_AI_TOOL_CALL_RESULT, serialised)
            if self._capture_content:
                self._content.record_tool_result(call_id, content)
        self._spans.end_tool(
            sid,
            call_id,
            _EndOptions(
                status="error" if is_error else "ok",
                error_type="tool_execution_error" if is_error else None,
            ),
        )

    def _on_usage_snapshot(self, event: TelemetryEvent) -> None:
        sid = event.session_id
        self._ensure_invoke_agent(sid)
        model = event.payload.get("model")
        if model:
            self._remember(self._session_model, sid, str(model))
        snap = _normalize_usage(event.payload)
        self._usage.ingest(sid, snap, self._metric_ctx(sid))

    def _on_session_ended(self, event: TelemetryEvent) -> None:
        sid = event.session_id
        # Ensure a root exists even for a childless session, so its identity +
        # final usage still export.
        self._ensure_invoke_agent(sid)
        is_error = bool(event.payload.get("is_error"))
        error_type = (
            str(event.payload.get("error_type") or event.payload.get("subtype") or "error")
            if is_error
            else None
        )
        # Stamp the final turn's usage delta on the root, flush content, close.
        root = self._spans.get_invoke_agent(sid)
        if root is not None:
            root.set_attributes(self._usage.turn_span_attributes(sid))
        self._flush_turn_content(sid)
        self._spans.end_all_for_session(
            sid,
            _EndOptions(
                status="error" if is_error else "ok",
                error_type=error_type,
                error_message=event.payload.get("error_message"),
            ),
        )
        duration_s = float(event.payload.get("duration_ms", 0.0) or 0.0) / 1000.0
        self._usage.end_session(
            sid,
            self._metric_ctx(sid),
            error_type=error_type,
            duration_s=duration_s if duration_s > 0 else None,
        )
        # Drop per-session content; retain identity (FIFO-bounded) so later
        # turns of a still-live session keep their agent name/provider.
        self._content.clear_session(sid)

    def _on_user_message(self, _event: TelemetryEvent) -> None:
        # OtelSink ignores user_message; turn_started is the authoritative
        # per-turn boundary (AgentOS sinks consume user_message instead).
        return

    def _on_system_message(self, _event: TelemetryEvent) -> None:
        return

    def _on_policy_decision(self, event: TelemetryEvent) -> None:
        sid = event.session_id
        attrs: dict[str, Any] = {
            "policy.engine": event.payload.get("engine", ""),
            "policy.decision": event.payload.get("decision", ""),
            "policy.reason": event.payload.get("reason", ""),
            "policy.latency_ms": float(event.payload.get("latency_ms", 0.0) or 0.0),
            "policy.tool_name": event.payload.get("tool_name", ""),
        }
        tool_name = str(event.payload.get("tool_name") or "")
        # Prefer an open execute_tool span with a matching name; else annotate
        # the invoke_agent root.
        target_span = None
        if tool_name:
            for rec in self._spans.open_tool_recs(sid):
                if rec.tool_name == tool_name:
                    target_span = rec.span
                    break
        if target_span is None:
            target_span = self._spans.get_invoke_agent(sid)
        if target_span is not None:
            try:
                target_span.add_event("policy_decision", attributes=attrs)
            except Exception:  # noqa: BLE001
                logger.debug("policy_decision span event failed", exc_info=True)
            return
        # No active span — emit a standalone span so the decision is captured.
        try:
            span = self._tracer.start_span(name=f"policy_decision {tool_name or 'tool'}")
            for k, v in attrs.items():
                span.set_attribute(k, v)
            span.set_attribute(GEN_AI_CONVERSATION_ID, sid)
            span.end()
        except Exception:  # noqa: BLE001
            logger.debug("policy_decision standalone span failed", exc_info=True)


# Map kind → instance method. Resolved at module import; no per-event isinstance.
_DISPATCH: dict[str, Any] = {
    "session_started": OtelSink._on_session_started,
    "turn_started": OtelSink._on_turn_started,
    "assistant_message": OtelSink._on_assistant_message,
    "tool_use": OtelSink._on_tool_use,
    "tool_result": OtelSink._on_tool_result,
    "usage_snapshot": OtelSink._on_usage_snapshot,
    "session_ended": OtelSink._on_session_ended,
    "user_message": OtelSink._on_user_message,
    "system_message": OtelSink._on_system_message,
    "policy_decision": OtelSink._on_policy_decision,
}


def _infer_exporter(endpoint: str | None) -> str:
    return "otlp-http" if endpoint else "console"


def _signal_url(endpoint: str | None, signal: str) -> str | None:
    """Append ``/v1/<signal>`` if ``endpoint`` looks like a base URL.

    OTLP HTTP exporters treat a non-None ``endpoint`` constructor arg as the
    full URL — no automatic path appending — so passing the documented base
    URL (e.g. New Relic's ``https://otlp.nr-data.net``) produces 404s. When
    the caller has already supplied a signal-specific URL, return it
    unchanged so we never double-append.
    """
    if endpoint is None:
        return None
    e = endpoint.rstrip("/")
    if any(e.endswith(suffix) for suffix in ("/v1/traces", "/v1/metrics", "/v1/logs")):
        return endpoint
    return f"{e}/v1/{signal}"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_otlp_headers_env(raw: str | None) -> dict[str, str]:
    """Parse ``OTEL_EXPORTER_OTLP_HEADERS`` per the OTel env-spec format.

    Comma-separated ``key=value`` pairs; whitespace around either side is
    stripped. Lines without an ``=`` are dropped silently.
    """
    if not raw:
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        k, _, v = pair.partition("=")
        k = k.strip()
        v = v.strip()
        if k:
            out[k] = v
    return out


_VALID_TEMPORALITIES: frozenset[str] = frozenset(("delta", "cumulative"))


def _resolve_temporality(value: Temporality | str | None) -> Temporality:
    """Pick a temporality, falling back through env then to the default.

    Default is ``"delta"`` because every major SaaS OTLP intake (New Relic,
    Datadog direct, Honeycomb) requires delta for histograms — cumulative
    silently ingests but renders as zero. Users running their own collector
    can opt into cumulative.

    NOTE (parity item 6f, DEFERRED): TS @computeragent/observability currently
    defaults to cumulative. Reconciling the two defaults is parked for a
    separate research/discussion; do not change this without that decision.
    """
    candidate = value or os.environ.get("COMPUTERAGENT_OTEL_TEMPORALITY") or "delta"
    if candidate not in _VALID_TEMPORALITIES:
        return "delta"
    return candidate  # type: ignore[return-value]


def _delta_temporality_map() -> dict[Any, AggregationTemporality]:
    return {
        _SDKCounter: AggregationTemporality.DELTA,
        _SDKUpDownCounter: AggregationTemporality.DELTA,
        _SDKHistogram: AggregationTemporality.DELTA,
        _SDKObservableCounter: AggregationTemporality.DELTA,
        _SDKObservableUpDownCounter: AggregationTemporality.DELTA,
        _SDKObservableGauge: AggregationTemporality.DELTA,
    }


def _build_tracer_provider(
    *,
    service_name: str,
    exporter: str,
    endpoint: str | None,
    headers: dict[str, str],
) -> TracerProvider:
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    if exporter == "otlp-http":
        span_exporter = HttpSpanExporter(
            endpoint=_signal_url(endpoint, "traces"),
            headers=headers or None,
        )
        provider.add_span_processor(BatchSpanProcessor(span_exporter))
    else:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    return provider


def _build_meter_provider(
    *,
    service_name: str,
    exporter: str,
    endpoint: str | None,
    headers: dict[str, str],
    temporality: Temporality = "delta",
) -> MeterProvider:
    resource = Resource.create({"service.name": service_name})
    metric_exporter: Any
    if exporter == "otlp-http":
        kwargs: dict[str, Any] = {
            "endpoint": _signal_url(endpoint, "metrics"),
            "headers": headers or None,
        }
        if temporality == "delta":
            kwargs["preferred_temporality"] = _delta_temporality_map()
        metric_exporter = HttpMetricExporter(**kwargs)
    else:
        metric_exporter = ConsoleMetricExporter()
    reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=5000)
    return MeterProvider(resource=resource, metric_readers=[reader], views=_genai_views())


def _build_logger_provider(
    *,
    service_name: str,
    exporter: str,
    endpoint: str | None,
    headers: dict[str, str],
) -> LoggerProvider:
    resource = Resource.create({"service.name": service_name})
    provider = LoggerProvider(resource=resource)
    if exporter == "otlp-http":
        log_exporter = HttpLogExporter(
            endpoint=_signal_url(endpoint, "logs"),
            headers=headers or None,
        )
        provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    else:
        provider.add_log_record_processor(SimpleLogRecordProcessor(ConsoleLogExporter()))
    return provider


def _genai_views() -> list[View]:
    """Explicit-bucket histogram Views mirroring the TS provider, so the spec
    bucket boundaries apply on whichever code path emits the histogram (the
    SDK's default exponential aggregation would otherwise win)."""
    return [
        View(
            instrument_name=METRIC_TOKEN_USAGE,
            aggregation=ExplicitBucketHistogramAggregation(list(TOKEN_USAGE_BUCKETS)),
        ),
        View(
            instrument_name=METRIC_OPERATION_DURATION,
            aggregation=ExplicitBucketHistogramAggregation(list(OPERATION_DURATION_BUCKETS)),
        ),
        View(
            instrument_name=METRIC_COST_USD,
            aggregation=ExplicitBucketHistogramAggregation(list(COST_USD_BUCKETS)),
        ),
    ]
