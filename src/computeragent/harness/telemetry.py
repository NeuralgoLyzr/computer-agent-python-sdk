"""The telemetry pipeline a ComputerAgent sends through its egress routes.

* ``telemetry_url`` — OTLP (traces, metrics, logs) via
  :class:`~computeragent.telemetry.sinks.otel.OtelSink`.
* ``events_url`` — the AgentOS dashboard events via
  :class:`~computeragent.telemetry.sinks.agentos_http.AgentOSHttpSink`, posted
  to ``{events_url}/events``.

Both carry the caller's key on ``x-api-key`` for the egress proxy to swap.
Either may be unset, which turns that output off: there is no fallback to the
``AGENTOS_*`` or ``OTEL_EXPORTER_OTLP_ENDPOINT`` destinations, which would send
telemetry around the egress proxy.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ..telemetry.pipeline import Pipeline, TelemetrySink

logger = logging.getLogger("computeragent.harness")


def build_pipeline(
    *,
    telemetry_url: str | None,
    events_url: str | None,
    api_key: Callable[[], str],
) -> Pipeline:
    """A pipeline with one sink per configured route, and none otherwise."""
    sinks: list[TelemetrySink] = []
    if telemetry_url:
        try:
            from ..telemetry.sinks.otel import OtelSink
        except ImportError:
            logger.warning(
                "telemetry_url is set but OTLP export needs the [otel] extra "
                "(pip install 'computer-agent-py[otel]'); no traces will be sent"
            )
        else:
            # The OTLP exporters fix their headers when they are built, so the
            # key is read once here.
            sinks.append(
                OtelSink(
                    exporter="otlp-http",
                    endpoint=telemetry_url,
                    headers={"x-api-key": api_key()},
                )
            )
    if events_url:
        from ..telemetry.sinks.agentos_http import AgentOSHttpSink

        sinks.append(AgentOSHttpSink(url=f"{events_url}/events", api_key=api_key))
    return Pipeline(middleware=[], sinks=sinks, fail_open=False)


__all__ = ["build_pipeline"]
