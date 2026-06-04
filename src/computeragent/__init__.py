"""``computeragent`` — drop-in replacement for ``claude_agent_sdk``
PLUS a harness-based agent stack (added in 0.2.0).

Every public symbol from ``claude_agent_sdk`` is re-exported here with
identity preserved (``computeragent.ResultMessage is
claude_agent_sdk.ResultMessage``). ``query`` and ``ClaudeSDKClient`` are
wrapped so each message flows through a telemetry pipeline with
configurable PII redaction / guardrails and pluggable sinks
(OpenTelemetry, AgentOS registry).

Two modes:

1. **Drop-in proxy** (0.1.x, unchanged) — single-import swap::

       # before
       from claude_agent_sdk import ClaudeAgentOptions, query
       # after
       from computeragent import ClaudeAgentOptions, query

2. **Harness** (new in 0.2.0) — mirror of the TypeScript ComputerAgent
   stack with four orthogonal swappable axes (engine / substrate /
   session-store / identity-loader)::

       from computeragent import ComputerAgent, run_task

       async with ComputerAgent(source=Path.cwd()) as agent:
           result = await agent.chat("List the files in this directory")

The two modes co-exist. 0.1.x callers see no behaviour change.
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

__version__ = "0.2.1"
upstream_version = getattr(_cas, "__version__", "unknown")

# Override the two entry points with the proxied versions. Same signatures,
# same yielded types — but each yielded message also flows through the
# telemetry pipeline.
from ._proxy.client import ClaudeSDKClient  # noqa: E402
from ._proxy.query import query  # noqa: E402

# Harness surface (new in 0.2.0). Importing pulls the registries which
# register the built-in engines / substrate / session store / identity
# loader as a side effect — that's intentional so string-label
# resolution works without an explicit ``register_*`` call.
from .harness import (  # noqa: E402
    ChatHandle,
    ChatInput,
    ChatResult,
    ComputerAgent,
    HarnessError,
    HarnessProtocolError,
    UnknownEngineError,
    UnknownIdentityLoaderError,
    UnknownSessionStoreError,
    UnknownSubstrateError,
    UsageRollup,
    run_task,
)

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
from .protocol_types import (  # noqa: E402
    AgentManifest,
    EngineCapabilities,
    EngineContext,
    EngineDriver,
    EngineEvent,
    IdentityLoader,
    IdentitySource,
    PermissionDecision,
    PermissionRequest,
    PolicyDecider,
    SessionStore,
    SessionStoreEntry,
    Substrate,
    SubstrateHandle,
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
    # harness layer (0.2.0)
    "AgentManifest",
    "ChatHandle",
    "ChatInput",
    "ChatResult",
    "ComputerAgent",
    "EngineCapabilities",
    "EngineContext",
    "EngineDriver",
    "EngineEvent",
    "HarnessError",
    "HarnessProtocolError",
    "IdentityLoader",
    "IdentitySource",
    "PermissionDecision",
    "PermissionRequest",
    "PolicyDecider",
    "SessionStore",
    "SessionStoreEntry",
    "Substrate",
    "SubstrateHandle",
    "UnknownEngineError",
    "UnknownIdentityLoaderError",
    "UnknownSessionStoreError",
    "UnknownSubstrateError",
    "UsageRollup",
    "run_task",
]
