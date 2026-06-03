"""Telemetry surface — pipeline, middleware, sinks, configuration."""

from __future__ import annotations

from .config import configure, get_config, get_pipeline, shutdown
from .event import EventKind, TelemetryEvent, new_session_id
from .middleware.guardrails import GuardrailFilter
from .middleware.pii import PiiRedactor
from .pipeline import Pipeline, TelemetryMiddleware, TelemetrySink

__all__ = [
    "EventKind",
    "GuardrailFilter",
    "PiiRedactor",
    "Pipeline",
    "TelemetryEvent",
    "TelemetryMiddleware",
    "TelemetrySink",
    "configure",
    "get_config",
    "get_pipeline",
    "new_session_id",
    "shutdown",
]
