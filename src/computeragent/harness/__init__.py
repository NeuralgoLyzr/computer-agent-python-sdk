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
model, same engine environment, same permission mode — except the engine runs in
this process instead of in a sandbox.

The agent itself is baked into the image: nothing is cloned. The engine runs in
the process's working directory (``options={"cwd": ...}`` overrides it), and the
agent is defined by the caller's ``options``. With ``gap_dir`` set, the SDK runs
in GAP mode instead and loads the agent from that directory's ``agent.yaml``.

Everything beyond the required arguments is an override for something the agent
document already answers. ``llm_proxy_url`` is needed only for an
Anthropic-shaped engine against a native (OpenAI-shaped) gateway.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from ..engines import resolve_engine
from ..protocol_types import (
    EngineDriver,
    PermissionRequest,
    PolicyDecider,
    SessionStore,
)
from ..session_stores import is_session_store, resolve_session_store
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
from .settings import SettingsError, resolve_settings
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
        self._cancelled = False
        self._result: ChatResult | None = None

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Any]:
        if self._drained or self._cancelled:
            return
        if self._stream is None:
            self._state, self._stream = await self._coordinator.run_chat(self._message)
        async for ev in self._stream:
            yield ev
        self._drained = True
        self._result = self._finalize_result()

    def __await__(self) -> Any:
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
        """Signal the engine to stop at its next checkpoint.

        Cancelling before the first iteration marks the handle so the run never
        starts — previously it was a silent no-op and the turn then ran to
        completion, which is the opposite of what was asked.
        """
        self._cancelled = True
        if self._state is None:
            self._drained = True
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


def _validate_gap_dir(gap_dir: str | Path) -> Path:
    """Resolve ``gap_dir`` to the GAP directory, or refuse it naming why.

    GAP mode is chosen explicitly, so a directory that is missing, is a file,
    or has no ``agent.yaml`` fails here rather than quietly running the agent
    as something other than the GAP agent the caller asked for.
    """
    if isinstance(gap_dir, str):
        if not gap_dir.strip():
            raise AgentResolveError("gap_dir is an empty string — pass the GAP directory's path.")
        gap_dir = Path(gap_dir)
    if not isinstance(gap_dir, Path):
        raise AgentResolveError(
            f"gap_dir must be a path (str or Path), got {type(gap_dir).__name__}."
        )
    if not gap_dir.exists():
        raise AgentResolveError(f"gap_dir does not exist: {gap_dir}")
    if not gap_dir.is_dir():
        raise AgentResolveError(f"gap_dir is not a directory: {gap_dir}")
    if not (gap_dir / "agent.yaml").is_file():
        raise AgentResolveError(
            f"gap_dir has no agent.yaml: {gap_dir}. GAP mode loads the agent from that file."
        )
    return gap_dir


class ComputerAgent:
    """Run an AgentOS-registered agent locally.

    One instance is one logical session: ``session_started`` fires on the first
    chat and ``session_ended`` on dispose, so N chats collapse into one
    conversation. Use ``async with`` so the session closes deterministically.
    """

    def __init__(
        self,
        *,
        # ── required: which agent, where, as whom, and run by what ───────
        # Each comes from its argument or, when that is unset, its environment
        # variable (see `settings.ENV_VARS`); missing ones fail together.
        agent_id: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        # Engine name ("claude-agent-sdk" / "gitagent") or an EngineDriver
        # instance. Required: it selects the driver AND which gateway-env
        # branch is built, so inferring it from the agent document meant a
        # document change could silently re-shape ANTHROPIC_BASE_URL and the
        # model string under a caller who never asked for either.
        engine: str | EngineDriver | None = None,
        # ── needed only in specific cases ────────────────────────────────
        # The Anthropic-to-OpenAI translating proxy. Required when an
        # Anthropic-shaped engine runs against a native (OpenAI-shaped)
        # gateway, and unused otherwise — see `gateway_envs`.
        llm_proxy_url: str | None = None,
        # GAP mode: load the agent from this directory's agent.yaml (baked
        # into the image). Unset, the agent is the caller's `options`.
        gap_dir: str | Path | None = None,
        # ── overrides for what the agent document already answers ────────
        # Merged over whatever the identity loader resolves — caller wins.
        options: Mapping[str, Any] | None = None,
        envs: Mapping[str, str] | None = None,
        session_store: str | SessionStore | Mapping[str, Any] | None = None,
        session_id: str | None = None,
        agent_name: str | None = None,
        agent_description: str | None = None,
        policy_decider: PolicyDecider | None = None,
        on_tool_call: Callable[[PermissionRequest], Awaitable[dict[str, Any]]] | None = None,
        budget_max_usd: float | None = None,
        telemetry_pipeline: Pipeline | None = None,
        # Injection point for tests — an httpx-like client for the resolve call.
        _resolve_client: Any | None = None,
    ) -> None:
        # 0. Every setting from its argument or its environment variable.
        self._settings = resolve_settings(
            agent_id=agent_id, engine=engine, base_url=base_url, api_key=api_key, gap_dir=gap_dir
        )
        engine = self._settings.engine
        gap_dir = self._settings.gap_dir
        self._api_base = api_base(self._settings.base_url)

        # 1. Ask AgentOS what this agent is. Fails fast and by field name, so a
        #    shape this run mode can't honour never reaches a boot.
        self.agent: ResolvedAgent = resolve_agent(
            self._settings.agent_id,
            base_url=self._settings.base_url,
            api_key=self._settings.api_key(),
            client=_resolve_client,
        )

        # 2. Engine: always the caller's, never inferred. `resolve_engine` is
        #    the gate now — an unregistered name raises UnknownEngineError
        #    naming what IS registered, which covers the "harness has no Python
        #    implementation" case from the side that can actually fix it.
        resolved_engine = engine if isinstance(engine, EngineDriver) else resolve_engine(engine)
        engine_name = resolved_engine.name
        # A mismatch is legal — bringing your own engine is how an unsupported
        # harness gets run — but it is worth a line in the log, because it also
        # changes which gateway-env branch is built.
        if engine_name != self.agent.harness:
            logger.warning(
                "engine=%r overrides agent %s harness=%r; gateway envs follow the ENGINE",
                engine_name,
                self.agent.agent_id,
                self.agent.harness,
            )

        # 3. Where the model calls go. Raises here, naming the missing
        #    parameter, rather than surfacing as a 405 mid-run.
        gateway_envs = build_gateway_envs(
            self.agent,
            engine_name=engine_name,
            api_key=self._settings.api_key(),
            llm_proxy_url=llm_proxy_url,
        )
        merged_envs = merge_envs(envs, gateway_envs)
        merged_options = build_options(self.agent, engine_name=engine_name, caller_options=options)
        # `budget_max_usd` used to be carried to the engine context and read by
        # nothing. The Claude CLI enforces `max_budget_usd` itself, so map it
        # there and let the parameter mean what its name says. An explicit
        # option from the caller still wins.
        if budget_max_usd is not None:
            merged_options.setdefault("max_budget_usd", budget_max_usd)

        # 4. What the agent runs against. The agent is baked into the image:
        #    GAP mode reads it from `gap_dir`, otherwise the engine runs in the
        #    process's working directory with the caller's options.
        from ..identity import resolve_identity_loader

        if gap_dir is not None:
            workspace = _validate_gap_dir(gap_dir)
            identity_loader = resolve_identity_loader("gap")
        else:
            workspace = Path.cwd()
            identity_loader = resolve_identity_loader("passthrough")

        resolved_store: SessionStore | None = None
        if session_store is not None:
            if is_session_store(session_store):
                resolved_store = cast("SessionStore", session_store)
            else:
                resolved_store = resolve_session_store(
                    cast("str | Mapping[str, Any]", session_store)
                )

        # `on_tool_call` is only consulted by engines that emit permission
        # requests. On claude-agent-sdk gating runs through `policy_decider`,
        # so a caller who passes only this believes they have a tool gate and
        # has none — too dangerous to leave silent.
        if on_tool_call is not None and engine_name == "claude-agent-sdk":
            logger.warning(
                "on_tool_call is set but the %r engine never emits permission requests, so it will "
                "not be consulted. Use policy_decider= to gate tool calls on this engine.",
                engine_name,
            )

        self._coordinator: _CoordinatorLike = HarnessCoordinator(
            engine=resolved_engine,
            substrate=LocalSubstrate(),
            identity_loader=identity_loader,
            source=workspace,
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
            options_overlay=merged_options,
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
    "SettingsError",
    "UnknownEngineError",
    "UnknownIdentityLoaderError",
    "UnknownSessionStoreError",
    "UnknownSubstrateError",
    "UnsupportedAgentError",
    "UsageRollup",
]
