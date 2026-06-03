"""``computeragent`` — drop-in replacement for ``claude_agent_sdk``.

Every public symbol from ``claude_agent_sdk`` is re-exported here with identity
preserved (``computeragent.ResultMessage is claude_agent_sdk.ResultMessage``).
``query`` and ``ClaudeSDKClient`` are wrapped so that every message flowing
through the upstream stream also flows through a telemetry pipeline with
configurable PII-redaction / guardrail middleware and pluggable sinks
(OpenTelemetry, AgentOS registry).

Drop-in usage::

    # before
    from claude_agent_sdk import ClaudeAgentOptions, query
    from claude_agent_sdk.types import ResultMessage

    # after — every other line stays identical
    from computeragent import ClaudeAgentOptions, query
    from computeragent.types import ResultMessage
"""

from __future__ import annotations

import claude_agent_sdk as _cas

# Re-export every public symbol from the upstream package by walking its
# `__all__`. This is identity-preserving — `computeragent.ResultMessage is
# claude_agent_sdk.ResultMessage` evaluates True — so user-side
# `isinstance(msg, ResultMessage)` keeps working regardless of import origin.
# Skip `__version__` so our own version (set below) wins.
_upstream_all = [n for n in getattr(_cas, "__all__", []) if n != "__version__"]
for _name in _upstream_all:
    globals()[_name] = getattr(_cas, _name)

__version__ = "0.1.0"
upstream_version = getattr(_cas, "__version__", "unknown")

# Override the two entry points with the proxied versions. Same signatures,
# same yielded types — but each yielded message also flows through the
# telemetry pipeline.
from ._proxy.client import ClaudeSDKClient  # noqa: E402
from ._proxy.query import query  # noqa: E402

# Policy surface (v0.2 — tool-use authorization). Engines themselves
# (OpaPolicyEngine, CedarPolicyEngine) stay in ``computeragent.policy`` so
# users opt into Cedar's extra deliberately.
from .policy import (  # noqa: E402
    PolicyAction,
    PolicyDecision,
    PolicyEngine,
    PolicyInput,
    PolicyPrincipal,
    PolicyResource,
    PolicyResult,
    PolicyToolAuthorizer,
)

# Telemetry surface (additions beyond the drop-in mirror).
from .telemetry import (  # noqa: E402
    GuardrailFilter,
    PiiRedactor,
    Pipeline,
    TelemetryEvent,
    TelemetryMiddleware,
    TelemetrySink,
    configure,
    get_pipeline,
    shutdown,
)

__all__ = sorted({*_upstream_all, "__version__"}) + [
    # telemetry + policy add-ons (alphabetical at the tail so the public
    # symbol list stays scannable when grepped against `claude_agent_sdk.__all__`)
    "GuardrailFilter",
    "PiiRedactor",
    "Pipeline",
    "PolicyAction",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyInput",
    "PolicyPrincipal",
    "PolicyResource",
    "PolicyResult",
    "PolicyToolAuthorizer",
    "TelemetryEvent",
    "TelemetryMiddleware",
    "TelemetrySink",
    "configure",
    "get_pipeline",
    "shutdown",
]
