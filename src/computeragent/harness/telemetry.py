"""The telemetry pipeline a ComputerAgent sends through its egress route.

``telemetry_url`` — OTLP (traces, metrics, logs) via
:class:`~computeragent.telemetry.sinks.otel.OtelSink`, with the caller's key on
``x-api-key`` for the egress proxy to swap. Unset turns telemetry off: there is
no fallback to the ``AGENTOS_*`` or ``OTEL_EXPORTER_OTLP_ENDPOINT``
destinations, which would send telemetry around the egress proxy.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ..telemetry.pipeline import Pipeline, TelemetrySink

logger = logging.getLogger("computeragent.harness")


def build_pipeline(
    *,
    telemetry_url: str | None,
    api_key: Callable[[], str],
) -> Pipeline:
    """A pipeline with the OTLP sink when ``telemetry_url`` is set, else none."""
    sinks: list[TelemetrySink] = []
    if telemetry_url:
        try:
            from ..telemetry.sinks.otel import OtelSink
            from .egress_token import stamping_session
        except ImportError:
            logger.warning(
                "telemetry_url is set but OTLP export needs the [otel] extra "
                "(pip install 'computer-agent-py[otel]'); no traces will be sent"
            )
        else:
            # The session sets x-api-key from `api_key()` on every request and
            # stamps the newest sandbox token; exports are split under the
            # egress proxy's 1 MiB limit.
            sinks.append(
                OtelSink(
                    exporter="otlp-http",
                    endpoint=telemetry_url,
                    headers={},
                    session=stamping_session(api_key),
                )
            )
    return Pipeline(middleware=[], sinks=sinks, fail_open=False)


__all__ = ["build_pipeline"]
