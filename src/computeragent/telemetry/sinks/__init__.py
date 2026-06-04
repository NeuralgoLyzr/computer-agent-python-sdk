"""Telemetry sinks — OTel collector and AgentOS HTTP ingest.

Each sink is conditional on its extra. Import the sink class only after
installing the corresponding extra; otherwise the import raises ``ImportError``
with a clean install hint.
"""

from __future__ import annotations

__all__ = ["AgentOSHttpSink", "OtelSink"]


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    """Lazy import so ``from .sinks import OtelSink`` raises a clean ImportError
    only when the optional dep is actually missing."""
    if name == "OtelSink":
        from .otel import OtelSink as _OtelSink

        return _OtelSink
    if name == "AgentOSHttpSink":
        from .agentos_http import AgentOSHttpSink as _AgentOSHttpSink

        return _AgentOSHttpSink
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
