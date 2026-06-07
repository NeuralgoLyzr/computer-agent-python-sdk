"""Module-level pipeline singleton + ``configure()`` API.

Users either call :func:`configure` explicitly, or rely on the env-driven
defaults that :func:`get_pipeline` builds on first use. Both paths are safe
to interleave (later ``configure()`` replaces the lazy default).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from .pipeline import Pipeline, TelemetryMiddleware, TelemetrySink

logger = logging.getLogger("computeragent.telemetry")


# ──────────────────────────────────────────────────────────────────────────
# Globals
# ──────────────────────────────────────────────────────────────────────────

_PIPELINE: Pipeline | None = None
_CONFIG: dict[str, Any] = {}


def get_pipeline() -> Pipeline:
    """Return the active pipeline, lazy-building from env if not configured."""
    global _PIPELINE
    if _PIPELINE is None:
        _PIPELINE = _build_default_pipeline()
    return _PIPELINE


def configure(
    *,
    middleware: list[TelemetryMiddleware] | None = None,
    sinks: list[TelemetrySink] | None = None,
    service_name: str | None = None,
    exporter: Literal["console", "otlp-http"] | None = None,
    endpoint: str | None = None,
    headers: dict[str, str] | None = None,
    sample_rate: float = 1.0,
    capture_content: bool = False,
    capture_content_mode: Literal["events", "attributes", "both"] = "events",
    max_attribute_length: int = 4096,
    fail_open: bool = False,
    temporality: Literal["delta", "cumulative"] | None = None,
) -> Pipeline:
    """Programmatic configuration. Idempotent — replaces the active pipeline.

    Auto-attaches the bundled :class:`~computeragent.telemetry.sinks.otel.OtelSink`
    and :class:`~computeragent.telemetry.sinks.agentos_http.AgentOSHttpSink` if their
    extras are installed and not already in ``sinks``. Pass ``sinks=[]`` to
    disable all auto-attachment.

    ``temporality`` controls metric aggregation temporality. Defaults to
    delta (set by OtelSink) which is what every major SaaS OTLP intake
    requires — pass ``"cumulative"`` only when exporting to a collector that
    aggregates downstream.
    """
    global _PIPELINE, _CONFIG
    _CONFIG = {
        "service_name": service_name or os.environ.get("OTEL_SERVICE_NAME", "computeragent"),
        "exporter": exporter,
        "endpoint": endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
        "headers": headers
        if headers is not None
        else _parse_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")),
        "sample_rate": sample_rate,
        "capture_content": capture_content,
        "capture_content_mode": capture_content_mode,
        "max_attribute_length": max_attribute_length,
        "temporality": temporality,
    }

    effective_sinks: list[TelemetrySink] = list(sinks) if sinks is not None else _auto_sinks()
    _PIPELINE = Pipeline(
        middleware=list(middleware or []),
        sinks=effective_sinks,
        fail_open=fail_open,
    )
    return _PIPELINE


async def shutdown(timeout: float = 5.0) -> None:
    """Flush and close the active pipeline. Safe to call multiple times."""
    global _PIPELINE
    if _PIPELINE is None:
        return
    try:
        await _PIPELINE.aclose()
    finally:
        _PIPELINE = None


# ──────────────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────────────


def get_config() -> dict[str, Any]:
    """Expose the resolved config to sinks (read-only contract)."""
    return dict(_CONFIG)


def _build_default_pipeline() -> Pipeline:
    """Construct a pipeline from env vars only. No middleware by default."""
    global _CONFIG
    _CONFIG = {
        "service_name": os.environ.get("OTEL_SERVICE_NAME", "computeragent"),
        "exporter": None,
        "endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
        "headers": _parse_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")),
        "sample_rate": 1.0,
        "temporality": os.environ.get("COMPUTERAGENT_OTEL_TEMPORALITY"),
        "capture_content": _env_bool("COMPUTERAGENT_CAPTURE_CONTENT", default=False),
        "capture_content_mode": os.environ.get("COMPUTERAGENT_CAPTURE_CONTENT_MODE", "events"),
        "max_attribute_length": int(os.environ.get("COMPUTERAGENT_MAX_ATTRIBUTE_LENGTH", "4096")),
    }
    return Pipeline(middleware=[], sinks=_auto_sinks(), fail_open=False)


def _auto_sinks() -> list[TelemetrySink]:
    """Build the sink list implied by environment variables.

    OTel sink: on when ``COMPUTERAGENT_OTEL!=disabled`` (default on) AND the
    ``opentelemetry-*`` packages are importable. Falls back to console
    exporter when no OTLP endpoint is configured.

    AgentOS sink: on when an ingest URL resolves (from ``AGENTOS_DISCOVERY_URL``
    or an ``AGENTOS_INGEST_URL`` override) AND ``httpx`` is importable. POSTs
    telemetry events to the AgentOS server, which owns the Mongo writes (registry
    / logs / sessions / chat_sessions / agent_messages).
    """
    sinks: list[TelemetrySink] = []

    if os.environ.get("COMPUTERAGENT_OTEL", "").lower() != "disabled":
        try:
            from .sinks.otel import OtelSink  # noqa: WPS433

            sinks.append(OtelSink())
        except ImportError:
            logger.debug("OtelSink not attached (computer-agent-py[otel] not installed)")

    from .. import agentos

    ingest_url = agentos.ingest_url()
    if ingest_url:
        try:
            from .sinks.agentos_http import AgentOSHttpSink  # noqa: WPS433

            sinks.append(AgentOSHttpSink(url=ingest_url))
        except ImportError:
            logger.debug(
                "AgentOSHttpSink not attached (computer-agent-py[agentos] not installed)"
            )

    return sinks


def _parse_headers(raw: str | None) -> dict[str, str]:
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


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _reset_for_tests() -> None:
    """Test hook — drop the cached pipeline so the next call rebuilds it."""
    global _PIPELINE, _CONFIG
    _PIPELINE = None
    _CONFIG = {}
