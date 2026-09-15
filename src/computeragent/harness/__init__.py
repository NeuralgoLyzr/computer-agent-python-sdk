"""Public harness surface — :class:`ComputerAgent`, :class:`ChatHandle`,
:class:`ChatResult`.

One run mode: name an agent registered in AgentOS, and run it **locally**::

    async with ComputerAgent(
        base_url="http://localhost:8788/agentos/api",
        agent_id="6aa93f1b3817a13d3103d2c1",
        api_key="<lyzr workspace key>",
        llm_proxy_url="http://127.0.0.1:8786",
    ) as agent:
        result = await agent.chat("List the files in your working directory.")

The SDK fetches the agent's definition from AgentOS and reproduces what the
server's own dispatch path would have sent to a ComputerAgent server — same
source, same model, same engine environment, same permission mode — except the
engine runs in this process instead of in a sandbox.

Everything beyond the three required arguments is an override for something the
agent document already answers. Two are needed only in specific cases:
``llm_proxy_url`` for an Anthropic-shaped engine against a native (OpenAI-shaped)
gateway, and ``git_token`` for a private source.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..engines import resolve_engine
from ..protocol_types import (
    AgentManifest,
    EngineDriver,
    IdentitySource,
    PermissionRequest,
    PolicyDecider,
    SessionStore,
)
from ..session_stores import resolve_session_store
from ..substrates import LocalSubstrate
from ..telemetry.pipeline import Pipeline
from .agentos_agent import (
    AgentResolveError,
    ResolvedAgent,
    UnsupportedAgentError,
    api_base,
    resolve_agent,
)
from .coordinator import HarnessCoordinator, _SessionState
from .errors import (
    HarnessError,
    UnknownEngineError,
    UnknownIdentityLoaderError,
    UnknownSessionStoreError,
    UnknownSubstrateError,
)
from .gateway_envs import GatewayConfigError, build_gateway_envs, build_options, merge_envs
from .usage import UsageRollup

logger = logging.getLogger("computeragent.harness")

ChatInput = str | dict[str, Any] | list[dict[str, Any]] | AsyncIterable[dict[str, Any]]


@dataclass(frozen=True)
class ChatResult:
    """Resolved value of one chat session — what ``await handle`` returns."""

    session_id: str
    messages: tuple[Any, ...]
    usage: UsageRollup
    duration_ms: float


class _CoordinatorLike(Protocol):
    """The slice of a coordinator that :class:`ChatHandle` drives."""

    async def run_chat(self, message: Any) -> tuple[_SessionState, AsyncIterator[Any]]: ...
    async def dispose(self) -> None: ...


class ChatHandle:
    """Live handle to one in-flight chat session.

    ``async for ev in handle`` streams :class:`EngineEvent`s in order;
    ``await handle`` drains the stream and returns a :class:`ChatResult`. Both
    forms compose: a partial iteration before ``await`` resumes where iteration
    left off.

    The run is launched lazily on first iteration — calling ``chat()`` itself
    does not block.
    """

    def __init__(self, *, coordinator: _CoordinatorLike, message: Any) -> None:
        self._coordinator = coordinator
        self._message = message
        self._state: _SessionState | None = None
        self._stream: AsyncIterator[Any] | None = None
        self._drained = False
        self._result: ChatResult | None = None

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Any]:
        if self._drained:
            return
        if self._stream is None:
            self._state, self._stream = await self._coordinator.run_chat(self._message)
        async for ev in self._stream:
            yield ev
        self._drained = True
        self._result = self._finalize_result()

    def __await__(self):
        return self.result().__await__()

    async def result(self) -> ChatResult:
        """Drain remaining events and return the final :class:`ChatResult`.

        Memoised — repeat calls return the same object.
        """
        if self._result is not None:
            return self._result
        async for _ in self._iterate():
            pass
        assert self._result is not None
        return self._result

    def get_usage(self) -> UsageRollup:
        """Snapshot of token + cost totals so far. Safe to call mid-stream."""
        if self._state is None:
            return UsageRollup()
        return self._state.usage

    async def cancel(self) -> None:
        """Signal the engine to stop iterating at its next checkpoint."""
        if self._state is None:
            return
        self._state.cancel_event.set()
        with suppress(asyncio.QueueFull):
            self._state.user_queue.put_nowait(None)

    def _finalize_result(self) -> ChatResult:
        assert self._state is not None
        duration_ms = (time.monotonic() - self._state.started_at) * 1000.0
        return ChatResult(
            session_id=self._state.session_id,
            messages=tuple(self._state.messages),
            usage=self._state.usage,
            duration_ms=duration_ms,
        )


class _AutoIdentityLoader:
    """GAP when the workspace is a GAP repo, passthrough when it isn't.

    The choice can only be made after the source has been materialised, since it
    turns on whether ``agent.yaml`` is at the workspace root — and
    :class:`~computeragent.identity.gap.GapIdentityLoader` raises rather than
    degrading when it isn't. Deciding here keeps a plain (non-GAP) repo runnable
    instead of making the loader a thing callers must get right by hand.
    """

    name = "auto"

    async def load(
        self, *, source: IdentitySource, target_engine: str, workspace_path: Path
    ) -> AgentManifest:
        from ..identity import resolve_identity_loader

        is_gap = (workspace_path / "agent.yaml").is_file()
        loader = resolve_identity_loader("gap" if is_gap else "passthrough")
        logger.debug(
            "identity loader: %s (agent.yaml %s)", loader.name, "found" if is_gap else "absent"
        )
        return await loader.load(
            source=source, target_engine=target_engine, workspace_path=workspace_path
        )


class ComputerAgent:
    """Run an AgentOS-registered agent locally.

    One instance is one logical session: ``session_started`` fires on the first
    chat and ``session_ended`` on dispose, so N chats collapse into one
    conversation. Use ``async with`` so the workspace (and any temp clone)
    closes deterministically.
    """

    def __init__(
        self,
        *,
        # ── required: which agent, where, and as whom ────────────────────
        agent_id: str,
        base_url: str,
        api_key: str,
        # ── needed only in specific cases ────────────────────────────────
        # The Anthropic-to-OpenAI translating proxy. Required when an
        # Anthropic-shaped engine runs against a native (OpenAI-shaped)
        # gateway, and unused otherwise — see `gateway_envs`.
        llm_proxy_url: str | None = None,
        # Only a PRIVATE source needs one. When omitted the SDK tries AgentOS's
        # GitHub-App credential route, then clones anonymously.
        git_token: str | None = None,
        # ── overrides for what the agent document already answers ────────
        # Use this workspace instead of cloning the agent's own source. A local
        # Path, a git URL, or an inline manifest dict.
        source: IdentitySource | None = None,
        # Engine name or an EngineDriver instance. Defaults to the agent's
        # `harness`; an explicit value also selects which env branch is built.
        engine: str | EngineDriver | None = None,
        # Merged over whatever the identity loader resolves — caller wins.
        options: Mapping[str, Any] | None = None,
        envs: Mapping[str, str] | None = None,
        session_store: str | SessionStore | Mapping[str, Any] | None = None,
        session_id: str | None = None,
        project_key: str = "default",
        agent_name: str | None = None,
        agent_description: str | None = None,
        policy_decider: PolicyDecider | None = None,
        on_tool_call: Callable[[PermissionRequest], Awaitable[dict[str, Any]]] | None = None,
        budget_max_usd: float | None = None,
        telemetry_pipeline: Pipeline | None = None,
        # Injection point for tests — an httpx-like client for the resolve call.
        _resolve_client: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_base = api_base(base_url)

        # 1. Ask AgentOS what this agent is. Fails fast and by field name, so a
        #    shape this run mode can't honour never reaches a boot.
        self.agent: ResolvedAgent = resolve_agent(
            agent_id, base_url=base_url, api_key=api_key, client=_resolve_client
        )

        # 2. Engine: the agent's harness unless the caller names another.
        resolved_engine = (
            engine
            if isinstance(engine, EngineDriver)
            else resolve_engine(engine or self.agent.harness)
        )
        engine_name = resolved_engine.name

        # 3. Where the model calls go. Raises here, naming the missing
        #    parameter, rather than surfacing as a 405 mid-run.
        gateway_envs = build_gateway_envs(
            self.agent,
            engine_name=engine_name,
            api_key=api_key,
            llm_proxy_url=llm_proxy_url,
        )
        merged_envs = merge_envs(envs, gateway_envs)
        merged_options = build_options(self.agent, engine_name=engine_name, caller_options=options)

        # 4. What the agent runs against: the caller's workspace if they gave
        #    one, else the source the agent document names.
        effective_source = source if source is not None else self.agent.source
        if effective_source is None:
            raise AgentResolveError(
                f"agent {self.agent.agent_id} has no source and none was passed — give the run a "
                "workspace with source=Path('...') or register a source on the agent."
            )

        resolved_store: SessionStore | None = None
        if session_store is not None:
            resolved_store = (
                session_store
                if isinstance(session_store, SessionStore)
                else resolve_session_store(session_store)
            )

        self._coordinator: _CoordinatorLike = HarnessCoordinator(
            engine=resolved_engine,
            substrate=LocalSubstrate(),
            identity_loader=_AutoIdentityLoader(),
            source=effective_source,
            envs=merged_envs,
            session_store=resolved_store,
            session_id=session_id,
            agent_name=agent_name or self.agent.name,
            agent_description=agent_description,
            agent_id=self.agent.agent_id,
            policy_decider=policy_decider,
            on_permission_request=on_tool_call,
            budget_max_usd=budget_max_usd,
            telemetry_pipeline=telemetry_pipeline,
            project_key=project_key,
            options_overlay=merged_options,
            git_token=git_token,
            git_credential_api_base=self._api_base,
            git_credential_token=api_key,
        )

    async def __aenter__(self) -> ComputerAgent:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.dispose()

    async def dispose(self) -> None:
        """Tear down the workspace and flush telemetry."""
        await self._coordinator.dispose()

    def chat(self, message: ChatInput) -> ChatHandle:
        """Start one chat turn. Returns immediately; iterate or await the
        returned :class:`ChatHandle` to consume the engine output."""
        return ChatHandle(coordinator=self._coordinator, message=message)


__all__ = [
    "AgentResolveError",
    "ChatHandle",
    "ChatInput",
    "ChatResult",
    "ComputerAgent",
    "GatewayConfigError",
    "HarnessError",
    "ResolvedAgent",
    "UnknownEngineError",
    "UnknownIdentityLoaderError",
    "UnknownSessionStoreError",
    "UnknownSubstrateError",
    "UnsupportedAgentError",
    "UsageRollup",
]
