"""Public harness surface — :class:`ComputerAgent`, :func:`run_task`,
:class:`ChatHandle`, :class:`ChatResult`.

Mirrors the TypeScript ``@open-gitagent/sdk`` shape one-for-one so prior
TS users have zero translation cost when moving to Python::

    async with ComputerAgent(
        source=Path.cwd(),
        engine="claude-agent-sdk",
        runtime="local",
    ) as agent:
        result = await agent.chat("Hello")
        print(result.messages)

This module is strictly additive vs 0.1.3: none of the legacy
``query`` / ``ClaudeSDKClient`` exports are touched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from ..engines import resolve_engine
from ..identity import resolve_identity_loader
from ..protocol_types import (
    EngineDriver,
    EngineEvent,
    IdentityLoader,
    IdentitySource,
    PermissionRequest,
    PolicyDecider,
    SessionStore,
    Substrate,
)
from ..session_stores import resolve_session_store
from ..substrates import resolve_substrate
from ..telemetry.pipeline import Pipeline
from .coordinator import HarnessCoordinator, _SessionState
from .errors import (
    HarnessError,
    HarnessProtocolError,
    UnknownEngineError,
    UnknownIdentityLoaderError,
    UnknownSessionStoreError,
    UnknownSubstrateError,
)
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


class ChatHandle:
    """Live handle to one in-flight chat session.

    ``async for ev in handle`` streams :class:`EngineEvent`s in order.
    ``await handle`` drains the stream and returns a :class:`ChatResult`.
    Both forms compose: a partial iteration before ``await`` resumes
    where iteration left off.

    The actual engine run is launched lazily on first iteration — calling
    ``chat()`` itself does not block, mirroring the TS surface.
    """

    def __init__(
        self,
        *,
        coordinator: HarnessCoordinator,
        message: Any,
    ) -> None:
        self._coordinator = coordinator
        self._message = message
        self._state: _SessionState | None = None
        self._stream: AsyncIterator[EngineEvent] | None = None
        self._drained = False
        self._result: ChatResult | None = None

    def __aiter__(self) -> AsyncIterator[EngineEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[EngineEvent]:
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


class ComputerAgent:
    """The top-level harness client.

    Wires an engine + substrate + session store + identity loader together
    and exposes a multi-chat interface. ``async with`` is recommended so
    the substrate handle (and thus any temp workspace) closes deterministically.
    """

    def __init__(
        self,
        *,
        source: IdentitySource,
        engine: str | EngineDriver = "claude-agent-sdk",
        runtime: str | Substrate = "local",
        session_store: str | SessionStore | None = None,
        identity_loader: str | IdentityLoader = "passthrough",
        envs: Mapping[str, str] | None = None,
        session_id: str | None = None,
        agent_name: str | None = None,
        agent_description: str | None = None,
        model: str | None = None,
        options: Mapping[str, Any] | None = None,
        policy_decider: PolicyDecider | None = None,
        on_tool_call: Callable[[PermissionRequest], Awaitable[dict[str, Any]]] | None = None,
        budget_max_usd: float | None = None,
        telemetry_pipeline: Pipeline | None = None,
        project_key: str = "default",
    ) -> None:
        resolved_engine = engine if isinstance(engine, EngineDriver) else resolve_engine(engine)
        resolved_substrate = (
            runtime if isinstance(runtime, Substrate) else resolve_substrate(runtime)
        )
        resolved_loader = (
            identity_loader
            if isinstance(identity_loader, IdentityLoader)
            else resolve_identity_loader(identity_loader)
        )
        resolved_store: SessionStore | None
        if session_store is None:
            resolved_store = None
        elif isinstance(session_store, SessionStore):
            resolved_store = session_store
        else:
            resolved_store = resolve_session_store(session_store)

        merged_options: dict[str, Any] = dict(options or {})
        if model is not None and "model" not in merged_options:
            merged_options["model"] = model

        # ``source`` may already be a dict-manifest. Merge any kwargs-supplied
        # options into it so the identity loader sees a single shape.
        normalised_source: IdentitySource
        if isinstance(source, dict):
            patched = dict(source)
            existing_opts = dict(patched.get("options") or {})
            existing_opts.update(merged_options)
            if existing_opts:
                patched["options"] = existing_opts
            normalised_source = patched
        else:
            normalised_source = source

        self._coordinator = HarnessCoordinator(
            engine=resolved_engine,
            substrate=resolved_substrate,
            identity_loader=resolved_loader,
            source=normalised_source,
            envs=dict(envs or {}),
            session_store=resolved_store,
            session_id=session_id,
            agent_name=agent_name,
            agent_description=agent_description,
            policy_decider=policy_decider,
            on_permission_request=on_tool_call,
            budget_max_usd=budget_max_usd,
            telemetry_pipeline=telemetry_pipeline,
            project_key=project_key,
            # Pass merged_options through as an overlay — the coordinator
            # layers these on top of whatever the identity loader resolves
            # so explicit caller kwargs always win.
            options_overlay=merged_options,
        )

    async def __aenter__(self) -> ComputerAgent:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.dispose()

    async def dispose(self) -> None:
        """Tear down the substrate handle and flush telemetry."""
        await self._coordinator.dispose()

    def chat(self, message: ChatInput) -> ChatHandle:
        """Start one chat turn. Returns immediately; iterate / await the
        returned :class:`ChatHandle` to consume the engine output."""
        return ChatHandle(coordinator=self._coordinator, message=message)


async def run_task(
    *,
    message: ChatInput,
    source: IdentitySource,
    engine: str | EngineDriver = "claude-agent-sdk",
    runtime: str | Substrate = "local",
    session_store: str | SessionStore | None = None,
    identity_loader: str | IdentityLoader = "passthrough",
    envs: Mapping[str, str] | None = None,
    session_id: str | None = None,
    agent_name: str | None = None,
    agent_description: str | None = None,
    model: str | None = None,
    options: Mapping[str, Any] | None = None,
    policy_decider: PolicyDecider | None = None,
    on_tool_call: Callable[[PermissionRequest], Awaitable[dict[str, Any]]] | None = None,
    budget_max_usd: float | None = None,
    telemetry_pipeline: Pipeline | None = None,
    project_key: str = "default",
) -> ChatResult:
    """One-shot convenience: spin up :class:`ComputerAgent`, send one
    message, drain, dispose, return the :class:`ChatResult`."""
    async with ComputerAgent(
        source=source,
        engine=engine,
        runtime=runtime,
        session_store=session_store,
        identity_loader=identity_loader,
        envs=envs,
        session_id=session_id,
        agent_name=agent_name,
        agent_description=agent_description,
        model=model,
        options=options,
        policy_decider=policy_decider,
        on_tool_call=on_tool_call,
        budget_max_usd=budget_max_usd,
        telemetry_pipeline=telemetry_pipeline,
        project_key=project_key,
    ) as agent:
        return await agent.chat(message)


__all__ = [
    "ChatHandle",
    "ChatInput",
    "ChatResult",
    "ComputerAgent",
    "HarnessError",
    "HarnessProtocolError",
    "UnknownEngineError",
    "UnknownIdentityLoaderError",
    "UnknownSessionStoreError",
    "UnknownSubstrateError",
    "UsageRollup",
    "run_task",
]
