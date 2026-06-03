"""Telemetry sinks — OTel collector and AgentOS Mongo registry.

Each sink is conditional on its extra. Import the sink class only after
installing the corresponding extra; otherwise the import raises ``ImportError``
with a clean install hint.
"""

from __future__ import annotations

__all__ = ["AgentRegistrySink", "MongoMessageSink", "OtelSink"]


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    """Lazy import so ``from .sinks import OtelSink`` raises a clean ImportError
    only when the optional dep is actually missing."""
    if name == "OtelSink":
        from .otel import OtelSink as _OtelSink

        return _OtelSink
    if name == "AgentRegistrySink":
        from .agentos import AgentRegistrySink as _AgentRegistrySink

        return _AgentRegistrySink
    if name == "MongoMessageSink":
        from .message_archive import MongoMessageSink as _MongoMessageSink

        return _MongoMessageSink
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
