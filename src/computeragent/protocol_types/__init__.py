"""Protocol contracts + value types for the 0.2.0 harness layer.

The harness has four orthogonal swappable axes — engine, substrate, session
store, identity loader — mirroring the TypeScript ``ComputerAgent`` project.
Each axis is a :class:`typing.Protocol`; built-in plugins implement these
Protocols and are registered at import time. Third-party plugins can drop
in by implementing the same shape — no inheritance required.

The Protocols are runtime-checkable so a misconfigured plugin fails fast
at registry time rather than three turns into a chat session.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

# The session-store axis is the UPSTREAM contract, re-exported rather than
# redefined. The TS stores implement `SessionStore` from
# `@anthropic-ai/claude-agent-sdk`; its Python twin is what
# `ClaudeAgentOptions.session_store` accepts, so any store written against it
# is handed straight to the engine with no adapter in between. A local
# lookalike would have to be translated at the boundary and would drift.
#
# Note it is deliberately NOT runtime-checkable upstream: only `append` and
# `load` are required and call sites probe for the rest, so a store is
# duck-typed, never isinstance-checked.
from claude_agent_sdk.types import SessionKey as SessionKey
from claude_agent_sdk.types import SessionStore as SessionStore
from claude_agent_sdk.types import SessionStoreEntry as SessionStoreEntry

IdentitySource = Path | str | dict[str, Any]
"""Where the agent definition comes from.

* ``Path`` — local directory, used as workspace ``cwd``.
* ``str`` — filesystem path string normalised to ``Path``.
* ``dict`` — inline manifest, see :class:`AgentManifest` for the shape.

Nothing is cloned or fetched: the agent is baked into the image.
"""


@dataclass(frozen=True)
class AgentManifest:
    """What the identity loader resolves a source to.

    The harness threads ``options`` straight into the engine — the engine
    decides which keys it understands (``ClaudeAgentEngine`` accepts every
    ``ClaudeAgentOptions`` field; ``GitAgentEngine`` honours ``model``,
    ``system_prompt``, ``allowed_tools`` etc.).
    """

    name: str
    version: str = "0.0.0"
    options: Mapping[str, Any] = field(default_factory=dict)
    sha: str | None = None
    # Post-merge hardening hook (optional). Applied by the coordinator AFTER
    # caller options are overlaid, so loader-declared compliance constraints
    # (e.g. GAP human-in-the-loop → permission mode) win over caller intent.
    # Excluded from equality — callables compare by identity only.
    harden: Callable[[dict[str, Any]], dict[str, Any]] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class EngineCapabilities:
    """Self-describing flags the harness reads to gate optional features."""

    streaming_input: bool
    partial_messages: bool
    permission_callback: bool
    sessions: bool
    budget: bool


@dataclass(frozen=True)
class PermissionRequest:
    """One tool-call gate decision request, emitted by the engine."""

    call_id: str
    tool_name: str
    input: Any
    risk: Literal["low", "medium", "high", "destructive"] | None = None


PermissionDecision = dict[str, Any]
"""Shape matches ``claude_agent_sdk.types.PermissionResultAllow|Deny`` so
existing ``PolicyToolAuthorizer`` returns drop straight in.
"""


@dataclass
class EngineEvent:
    """Discriminated-union dataclass for everything an engine yields.

    The ``kind`` field is the discriminator. Only the fields named in the
    per-kind documentation below are guaranteed to be populated; others
    stay at their defaults (``None``).

    Kinds:
        * ``session_started`` — emitted by the harness coordinator, not by
          the engine. ``payload`` is the resolved :class:`AgentManifest`.
        * ``sdk_message`` — one upstream message from the underlying agent.
          ``payload`` is the raw message object (e.g. ``AssistantMessage``).
        * ``usage_snapshot`` — token + cost reading. ``cost_semantic``
          distinguishes ``"delta"`` (gitagent) from ``"cumulative"``
          (claude-agent-sdk's ``ResultMessage``).
        * ``permission_request`` — engine wants to call a tool and is
          asking the harness to decide. ``payload`` is a
          :class:`PermissionRequest`.
        * ``session_ended`` — emitted by the harness coordinator. ``payload``
          carries ``is_error``, ``duration_ms``, ``num_turns``,
          ``total_cost_usd``.
    """

    kind: Literal[
        "session_started",
        "sdk_message",
        "usage_snapshot",
        "permission_request",
        "session_ended",
    ]
    payload: Any = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cost_usd: float | None = None
    cost_semantic: Literal["cumulative", "delta"] | None = None


@dataclass
class EngineContext:
    """What an :class:`EngineDriver` sees on ``start_session``.

    The harness owns construction — engine implementations should treat
    every field as read-only and route mutations through the helpers
    documented on each field.
    """

    session_id: str
    manifest: AgentManifest
    workspace_path: Path
    envs: Mapping[str, str]
    user_message_queue: AsyncIterable[dict[str, Any]]
    on_permission_request: Callable[[PermissionRequest], Awaitable[PermissionDecision]]
    cancel_event: asyncio.Event
    session_store: SessionStore | None = None
    policy_decider: PolicyDecider | None = None
    budget_max_usd: float | None = None
    #: The longest one ``policy_decider`` call may take, in seconds, when the
    #: harness knows it. An engine that runs the decider under a deadline of
    #: its own must allow more, so the decider's timeout (a deny) decides.
    policy_timeout_s: float | None = None
    #: The engine-side session id of the PREVIOUS turn, when there was one.
    #: An engine that can continue a conversation should resume from it; each
    #: turn otherwise starts a fresh engine session, and a multi-turn chat
    #: would have no memory of the turn before.
    resume_session_id: str | None = None


@dataclass
class SubstrateHandle:
    """What a :class:`Substrate` returns from ``boot``.

    ``workspace_path`` is where the engine should rest the agent's cwd.
    ``close`` is awaited when the harness disposes the substrate; idempotent.
    """

    workspace_path: Path
    close: Callable[[], Awaitable[None]]


# ── Protocols ──────────────────────────────────────────────────────────────


@runtime_checkable
class EngineDriver(Protocol):
    """Drives the underlying agent loop.

    ``start_session`` is an async generator: each value yielded becomes
    one :class:`EngineEvent`. The harness converts ``sdk_message`` events
    through ``TelemetryEvent.from_message`` and pushes them through the
    standard telemetry pipeline — engines do NOT touch the pipeline.
    """

    name: str
    capabilities: EngineCapabilities

    def start_session(self, ctx: EngineContext) -> AsyncIterator[EngineEvent]: ...


@runtime_checkable
class Substrate(Protocol):
    """Provides the workspace (cwd) the agent runs against.

    ``boot`` resolves the ``source`` into a concrete filesystem location.
    Local: pass-through of a directory that already exists.
    """

    name: str

    async def boot(
        self,
        *,
        source: IdentitySource,
        envs: Mapping[str, str],
    ) -> SubstrateHandle: ...


@runtime_checkable
class IdentityLoader(Protocol):
    """Resolves an :class:`IdentitySource` into an :class:`AgentManifest`.

    The MVP ships ``PassthroughLoader`` (treats inline-dict sources as the
    manifest directly). GAP-style git-repo loaders land in 0.3.0.
    """

    name: str

    async def load(
        self,
        *,
        source: IdentitySource,
        target_engine: str,
        workspace_path: Path,
    ) -> AgentManifest: ...


@runtime_checkable
class PolicyDecider(Protocol):
    """Structurally compatible with ``claude_agent_sdk.types.CanUseTool``.

    The existing :class:`computeragent.policy.PolicyToolAuthorizer` already
    matches this signature — users construct one in 0.1.x style and pass
    it to ``ComputerAgent(policy_decider=...)`` with zero glue.
    """

    async def __call__(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: Any,
    ) -> Any: ...


__all__ = [
    "AgentManifest",
    "EngineCapabilities",
    "EngineContext",
    "EngineDriver",
    "EngineEvent",
    "IdentityLoader",
    "IdentitySource",
    "PermissionDecision",
    "PermissionRequest",
    "PolicyDecider",
    "SessionStore",
    "SessionStoreEntry",
    "Substrate",
    "SubstrateHandle",
]
