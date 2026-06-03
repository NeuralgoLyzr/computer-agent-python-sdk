"""``OtelSink`` — emit GenAI-semconv-compliant OpenTelemetry spans + metrics.

Span tree matches the TS ``@computeragent/observability`` package so polyglot
deployments produce one coherent trace tree in the same backend::

    invoke_agent <agent_name>   [gen_ai.conversation.id = session_id,
                                 gen_ai.system = "claude-agent-sdk",
                                 gen_ai.agent.name = ...]
    └── chat <model>            [gen_ai.request.model = ...]
        └── execute_tool <name> [gen_ai.tool.name = ...,
                                 gen_ai.tool.call.id = ...]

Metrics::

    gen_ai.client.token.usage           (histogram, unit "{token}")
        dims: gen_ai.operation.name, gen_ai.request.model, gen_ai.token.type
    gen_ai.client.operation.duration    (histogram, unit "s")
    computeragent.usage.cost_usd        (histogram, unit "{usd}") — custom
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import TYPE_CHECKING, Any, Literal

try:
    from opentelemetry import metrics, trace  # noqa: F401 - probe imports only
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter as HttpMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter as HttpSpanExporter,
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
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )
except ImportError as _exc:  # pragma: no cover - guarded by lazy export
    raise ImportError(
        "OtelSink requires the [otel] extra. Install with: pip install 'computer-agent-py[otel]'"
    ) from _exc

Temporality = Literal["delta", "cumulative"]

if TYPE_CHECKING:
    from opentelemetry.trace import Span

    from ..event import TelemetryEvent

logger = logging.getLogger("computeragent.telemetry")


# GenAI semconv attribute keys — pinned literals to guarantee parity with the
# TS @computeragent/observability emission. The semconv Python package's
# constants for these keys move between alpha versions; literals are safer.
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_AGENT_DESCRIPTION = "gen_ai.agent.description"
GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GEN_AI_TOKEN_TYPE = "gen_ai.token.type"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS = "gen_ai.usage.cache_read_input_tokens"
GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS = "gen_ai.usage.cache_creation_input_tokens"

METRIC_TOKEN_USAGE = "gen_ai.client.token.usage"
METRIC_OPERATION_DURATION = "gen_ai.client.operation.duration"
METRIC_COST_USD = "computeragent.usage.cost_usd"


class OtelSink:
    """Translate :class:`TelemetryEvent` into OTel spans + metric measurements.

    Configures its own ``TracerProvider`` and ``MeterProvider`` on first use
    unless ``tracer_provider`` / ``meter_provider`` is passed in (tests use
    this hook to attach in-memory exporters).
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

        # Content capture — opt-in. When on, the sink writes prompt /
        # completion / tool input / tool output to spans per the GenAI
        # semconv. Middleware (PiiRedactor, GuardrailFilter) has already
        # mutated the payload by this point, so what lands on spans is the
        # redacted projection — never raw content from the agent stream.
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
        # limits. Honors the same env that GuardrailFilter consumes.
        try:
            self._max_attr_length = int(
                cfg.get("max_attribute_length")
                or os.environ.get("COMPUTERAGENT_MAX_ATTRIBUTE_LENGTH", "8192")
            )
        except (TypeError, ValueError):
            self._max_attr_length = 8192

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
        self._tracer = self._tracer_provider.get_tracer("computeragent")
        self._meter = self._meter_provider.get_meter("computeragent")

        self._token_usage_hist = self._meter.create_histogram(
            METRIC_TOKEN_USAGE, unit="{token}", description="GenAI token usage"
        )
        self._duration_hist = self._meter.create_histogram(
            METRIC_OPERATION_DURATION, unit="s", description="GenAI operation duration"
        )
        self._cost_hist = self._meter.create_histogram(
            METRIC_COST_USD, unit="{usd}", description="Inference cost in USD"
        )

        # session_id → (invoke_agent span, chat span, active execute_tool span)
        # Spans are held open across events for the same session/turn.
        self._invoke_spans: dict[str, Span] = {}
        self._chat_spans: dict[str, Span] = {}
        self._tool_spans: dict[str, dict[str, Span]] = {}  # session → tool_use_id → span

    # ── lifecycle ----------------------------------------------------------

    async def emit(self, event: TelemetryEvent) -> None:
        handler = _DISPATCH.get(event.kind)
        if handler is None:
            return
        handler(self, event)

    async def flush(self, timeout: float = 5.0) -> None:
        # OTel SDK's flush takes milliseconds. Convert + fan out.
        ms = int(timeout * 1000)
        try:
            self._tracer_provider.force_flush(timeout_millis=ms)
        except Exception:  # noqa: BLE001
            logger.debug("tracer_provider.force_flush failed", exc_info=True)
        try:
            self._meter_provider.force_flush(timeout_millis=ms)
        except Exception:  # noqa: BLE001
            logger.debug("meter_provider.force_flush failed", exc_info=True)

    async def aclose(self) -> None:
        await self.flush()
        try:
            self._tracer_provider.shutdown()
        except Exception:  # noqa: BLE001
            logger.debug("tracer_provider.shutdown failed", exc_info=True)
        try:
            self._meter_provider.shutdown()
        except Exception:  # noqa: BLE001
            logger.debug("meter_provider.shutdown failed", exc_info=True)

    # ── handlers (one per event kind) --------------------------------------

    def _emit_content(
        self,
        span: Any,
        role: str,
        content: str,
        attr_prefix: str,
        event_name: str,
    ) -> None:
        """Write content to ``span`` per ``capture_content_mode``.

        * ``attributes`` — ``<attr_prefix>.role`` + ``<attr_prefix>.content``
          per the GenAI semconv 1.27 ``gen_ai.prompt.{i}.*`` / ``gen_ai.completion.{i}.*``
          pattern. Easy to query in NRQL, lossy beyond ~32 KB.
        * ``events`` — ``span.add_event(event_name, {role, content})``.
          Preserves multi-message history without overwriting prior attrs.
        * ``both`` — write both shapes. Useful while migrating backends.

        Content is truncated to ``max_attribute_length`` chars to stay
        under OTLP per-attribute limits. The middleware
        (``PiiRedactor`` / ``GuardrailFilter``) has already mutated the
        payload by this point, so the redacted projection is what lands.
        """
        if not self._capture_content or not content:
            return
        text = (
            content
            if len(content) <= self._max_attr_length
            else (content[: self._max_attr_length - 14] + "...[truncated]")
        )
        if self._capture_mode in ("attributes", "both"):
            span.set_attribute(f"{attr_prefix}.role", role)
            span.set_attribute(f"{attr_prefix}.content", text)
        if self._capture_mode in ("events", "both"):
            try:
                span.add_event(event_name, attributes={"role": role, "content": text})
            except Exception:  # noqa: BLE001 — never break a session for an event-emit
                logger.debug("add_event(%s) failed", event_name, exc_info=True)

    def _parent_ctx(self, session_id: str) -> Any:
        """Build an OTel Context pinned to the active invoke_agent span so
        child spans (chat / execute_tool / policy_decision) nest correctly.

        ``invoke_agent`` is created via ``start_span`` (not
        ``start_as_current_span``) because its lifetime spans many event
        handler calls — we can't hold a ``with`` block across them. The
        trade-off is that the span is never the "current" span by default, so
        children would orphan into new traces unless we re-pin context here.
        """
        parent = self._invoke_spans.get(session_id)
        if parent is None:
            return None
        return trace.set_span_in_context(parent)

    def _on_session_started(self, event: TelemetryEvent) -> None:
        span = self._tracer.start_span(name=f"invoke_agent {event.agent_name or 'agent'}")
        span.set_attribute(GEN_AI_SYSTEM, "claude-agent-sdk")
        if event.agent_name:
            span.set_attribute(GEN_AI_AGENT_NAME, event.agent_name)
        if event.agent_description:
            span.set_attribute(GEN_AI_AGENT_DESCRIPTION, event.agent_description)
        span.set_attribute(GEN_AI_CONVERSATION_ID, event.session_id)
        span.set_attribute(GEN_AI_OPERATION_NAME, "invoke_agent")
        if event.payload.get("model"):
            span.set_attribute(GEN_AI_REQUEST_MODEL, str(event.payload["model"]))
        # Content capture — system prompt + initial user prompt, both on the
        # invoke_agent root so a single span query surfaces what the agent
        # was asked to do.
        system_prompt = event.payload.get("system_prompt")
        if isinstance(system_prompt, str) and system_prompt:
            self._emit_content(
                span,
                "system",
                system_prompt,
                "gen_ai.prompt.0",
                "gen_ai.system.message",
            )
        prompt = event.payload.get("prompt")
        if isinstance(prompt, str) and prompt and prompt != "<streaming>":
            # When the caller streams the prompt (AsyncIterable), the proxy
            # writes the sentinel "<streaming>" instead of the body — skip
            # capture in that case because there's nothing to record yet.
            self._emit_content(
                span,
                "user",
                prompt,
                "gen_ai.prompt.1",
                "gen_ai.user.message",
            )
        self._invoke_spans[event.session_id] = span

    def _on_assistant_message(self, event: TelemetryEvent) -> None:
        # Open or reuse a chat span; spec models chat as one span per LLM call,
        # but the upstream stream gives us one AssistantMessage per LLM call,
        # so open/close per event.
        chat = self._tracer.start_span(
            name=f"chat {event.payload.get('model') or 'unknown'}",
            context=self._parent_ctx(event.session_id),
        )
        chat.set_attribute(GEN_AI_OPERATION_NAME, "chat")
        if event.payload.get("model"):
            chat.set_attribute(GEN_AI_REQUEST_MODEL, str(event.payload["model"]))
        chat.set_attribute(GEN_AI_CONVERSATION_ID, event.session_id)
        text = event.payload.get("text")
        if isinstance(text, str) and text:
            self._emit_content(
                chat,
                "assistant",
                text,
                "gen_ai.completion.0",
                "gen_ai.assistant.message",
            )
        chat.end()

    def _on_tool_use(self, event: TelemetryEvent) -> None:
        tool_name = str(event.payload.get("tool_name") or "tool")
        span = self._tracer.start_span(
            name=f"execute_tool {tool_name}",
            context=self._parent_ctx(event.session_id),
        )
        span.set_attribute(GEN_AI_OPERATION_NAME, "execute_tool")
        span.set_attribute(GEN_AI_TOOL_NAME, tool_name)
        call_id = str(event.payload.get("tool_use_id") or "")
        if call_id:
            span.set_attribute(GEN_AI_TOOL_CALL_ID, call_id)
        span.set_attribute(GEN_AI_CONVERSATION_ID, event.session_id)
        # Tool arguments — JSON-encoded so multi-key inputs survive the
        # attribute-string projection.
        tool_input = event.payload.get("tool_input")
        if isinstance(tool_input, dict) and tool_input:
            import json

            try:
                serialised = json.dumps(tool_input, default=str)
            except (TypeError, ValueError):
                serialised = str(tool_input)
            self._emit_content(
                span,
                "tool",
                serialised,
                "gen_ai.tool.call.arguments",
                "gen_ai.tool.call.start",
            )
        self._tool_spans.setdefault(event.session_id, {})[call_id] = span

    def _on_tool_result(self, event: TelemetryEvent) -> None:
        call_id = str(event.payload.get("tool_use_id") or "")
        span = self._tool_spans.get(event.session_id, {}).pop(call_id, None)
        if span is None:
            return
        if event.payload.get("is_error"):
            span.set_attribute("error.type", "tool_error")
        # Tool output — agent-readable text projection. The block may be a
        # str OR a list[dict] (Anthropic tool-result content blocks); we
        # serialise dicts to JSON, leave strings alone.
        content = event.payload.get("content")
        if content:
            if isinstance(content, str):
                serialised = content
            else:
                import json

                try:
                    serialised = json.dumps(content, default=str)
                except (TypeError, ValueError):
                    serialised = str(content)
            self._emit_content(
                span,
                "tool",
                serialised,
                "gen_ai.tool.call.result",
                "gen_ai.tool.call.end",
            )
        span.end()

    def _on_usage_snapshot(self, event: TelemetryEvent) -> None:
        usage = event.payload.get("usage") or {}
        model = event.payload.get("model")
        common_dims = {
            GEN_AI_OPERATION_NAME: "chat",
            **({GEN_AI_REQUEST_MODEL: str(model)} if model else {}),
        }
        for token_type, key in (
            ("input", "input_tokens"),
            ("output", "output_tokens"),
            ("cache_read", "cache_read_input_tokens"),
            ("cache_creation", "cache_creation_input_tokens"),
        ):
            raw = usage.get(key)
            if raw is None:
                continue
            try:
                v = int(raw)
            except (TypeError, ValueError):
                continue
            self._token_usage_hist.record(
                v, attributes={**common_dims, GEN_AI_TOKEN_TYPE: token_type}
            )

        cost = event.payload.get("total_cost_usd")
        if cost is not None:
            with contextlib.suppress(TypeError, ValueError):
                self._cost_hist.record(float(cost), attributes=common_dims)

    def _on_session_ended(self, event: TelemetryEvent) -> None:
        # Close any tool spans still open for this session (defensive — should
        # already be closed by tool_result events).
        for span in self._tool_spans.pop(event.session_id, {}).values():
            try:
                span.end()
            except Exception:  # noqa: BLE001
                logger.debug("stray tool span end failed", exc_info=True)

        invoke = self._invoke_spans.pop(event.session_id, None)
        if invoke is None:
            return
        if event.payload.get("is_error"):
            invoke.set_attribute(
                "error.type",
                str(event.payload.get("error_type") or event.payload.get("subtype") or "error"),
            )
        usage = event.payload.get("usage") or {}
        for key, attr in (
            ("input_tokens", GEN_AI_USAGE_INPUT_TOKENS),
            ("output_tokens", GEN_AI_USAGE_OUTPUT_TOKENS),
            ("cache_read_input_tokens", GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS),
            ("cache_creation_input_tokens", GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS),
        ):
            v = usage.get(key)
            if isinstance(v, int):
                invoke.set_attribute(attr, v)
        # Final answer — surface the ResultMessage.result (or last assistant
        # text fallback) on the invoke_agent root so a single span query
        # returns the agent's response.
        result_text = event.payload.get("result")
        if isinstance(result_text, str) and result_text:
            self._emit_content(
                invoke,
                "assistant",
                result_text,
                "gen_ai.completion.final",
                "gen_ai.assistant.message",
            )
        invoke.end()
        duration_s = float(event.payload.get("duration_ms", 0.0) or 0.0) / 1000.0
        if duration_s > 0:
            self._duration_hist.record(
                duration_s, attributes={GEN_AI_OPERATION_NAME: "invoke_agent"}
            )

    def _on_user_message(self, _event: TelemetryEvent) -> None:
        # No span; user messages are inputs, recorded as conversation events
        # on the active invoke_agent span in a future iteration.
        return

    def _on_system_message(self, _event: TelemetryEvent) -> None:
        return

    def _on_policy_decision(self, event: TelemetryEvent) -> None:
        # Annotate the active execute_tool span for this session, if any.
        # Otherwise drop a span event onto the invoke_agent span so the
        # decision is still visible in the trace.
        attrs: dict[str, Any] = {
            "policy.engine": event.payload.get("engine", ""),
            "policy.decision": event.payload.get("decision", ""),
            "policy.reason": event.payload.get("reason", ""),
            "policy.latency_ms": float(event.payload.get("latency_ms", 0.0) or 0.0),
            "policy.tool_name": event.payload.get("tool_name", ""),
        }
        tool_name = str(event.payload.get("tool_name") or "")
        tool_spans = self._tool_spans.get(event.session_id, {})
        # Tool spans are keyed by tool_use_id, which we don't have here. Pick
        # the most-recently-opened span with a matching tool name.
        target_span = None
        for span in tool_spans.values():
            if getattr(span, "name", "").endswith(tool_name) and tool_name:
                target_span = span
                break
        if target_span is None:
            target_span = self._invoke_spans.get(event.session_id)
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
            span.set_attribute(GEN_AI_CONVERSATION_ID, event.session_id)
            span.end()
        except Exception:  # noqa: BLE001
            logger.debug("policy_decision standalone span failed", exc_info=True)


# Map kind → instance method. Resolved at module import; no per-event isinstance.
_DISPATCH: dict[str, Any] = {
    "session_started": OtelSink._on_session_started,
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
    return MeterProvider(resource=resource, metric_readers=[reader])
