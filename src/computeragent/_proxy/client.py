"""``ClaudeSDKClient`` — wraps upstream's class with a telemetry tap.

The wrapper holds an instance of the upstream client and forwards every
public method. ``receive_messages`` and ``receive_response`` re-yield messages
through the telemetry pipeline so multi-turn callers get the same
observability as :func:`computeragent.query`.

Control methods (``set_model``, ``get_context_usage``, etc.) pass straight
through — no telemetry for those today (can be added later as
``operation_started`` / ``operation_ended`` events).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import claude_agent_sdk
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

from ..telemetry import TelemetryEvent, get_pipeline, new_session_id
from .query import _derive_agent_description, _derive_agent_name

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, AsyncIterator

logger = logging.getLogger("computeragent")


class ClaudeSDKClient:
    """Drop-in for :class:`claude_agent_sdk.ClaudeSDKClient`.

    Same surface, same semantics, plus telemetry. The wrapper does not hold
    extra state beyond the upstream instance and a per-session id used to
    correlate emitted events.
    """

    def __init__(self, options: Any = None, transport: Any = None) -> None:
        kwargs: dict[str, Any] = {}
        if options is not None:
            kwargs["options"] = options
        if transport is not None:
            kwargs["transport"] = transport
        self._inner = claude_agent_sdk.ClaudeSDKClient(**kwargs)
        self._options = options
        self._session_id: str = new_session_id()
        # Upstream's own session id is captured for join-by-id but is NOT
        # used as the sink/event session_id (see _proxy.query for the
        # placeholder-vs-upstream rationale).
        self._upstream_session_id: str | None = None
        self._agent_name = _derive_agent_name(options)
        self._agent_description = _derive_agent_description(options)
        self._started_at: float | None = None
        self._last_result: ResultMessage | None = None
        self._last_assistant_text: str = ""
        self._connected = False

    # ── lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> ClaudeSDKClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        try:
            await self.disconnect()
        except Exception:  # noqa: BLE001
            logger.debug("disconnect in __aexit__ failed", exc_info=True)
        return False

    async def connect(self, prompt: str | AsyncIterable[dict[str, Any]] | None = None) -> None:
        pipeline = get_pipeline()
        self._started_at = time.monotonic()
        await pipeline.emit(
            TelemetryEvent.session_started(
                session_id=self._session_id,
                agent_name=self._agent_name,
                agent_description=self._agent_description,
                options=self._options,
                prompt=prompt if prompt is not None else "<connect>",
            )
        )
        if prompt is None:
            await self._inner.connect()
        else:
            await self._inner.connect(prompt)
        self._connected = True

    async def disconnect(self) -> None:
        if not self._connected:
            return
        try:
            await self._emit_session_ended()
        finally:
            self._connected = False
            try:
                await self._inner.disconnect()
            finally:
                try:
                    await get_pipeline().flush()
                except Exception:  # noqa: BLE001
                    logger.debug("flush in disconnect failed", exc_info=True)

    # ── streaming ----------------------------------------------------------

    async def query(
        self, prompt: str | AsyncIterable[dict[str, Any]], session_id: str = "default"
    ) -> None:
        await self._inner.query(prompt, session_id=session_id)

    async def receive_messages(self) -> AsyncIterator[Any]:
        pipeline = get_pipeline()
        async for msg in self._inner.receive_messages():
            real_sid = getattr(msg, "session_id", None)
            if (
                isinstance(real_sid, str)
                and real_sid
                and real_sid != self._session_id
                and self._upstream_session_id is None
            ):
                # Capture but don't rebind — see _proxy.query for rationale.
                self._upstream_session_id = real_sid
            for ev in TelemetryEvent.from_message(
                msg, session_id=self._session_id, agent_name=self._agent_name
            ):
                if self._agent_description is not None:
                    ev.agent_description = self._agent_description
                await pipeline.emit(ev)
            if isinstance(msg, AssistantMessage):
                for block in getattr(msg, "content", []) or []:
                    if isinstance(block, TextBlock):
                        self._last_assistant_text = block.text
            if isinstance(msg, ResultMessage):
                self._last_result = msg
            yield msg

    async def receive_response(self) -> AsyncIterator[Any]:
        pipeline = get_pipeline()
        async for msg in self._inner.receive_response():
            real_sid = getattr(msg, "session_id", None)
            if (
                isinstance(real_sid, str)
                and real_sid
                and real_sid != self._session_id
                and self._upstream_session_id is None
            ):
                self._upstream_session_id = real_sid
            for ev in TelemetryEvent.from_message(
                msg, session_id=self._session_id, agent_name=self._agent_name
            ):
                if self._agent_description is not None:
                    ev.agent_description = self._agent_description
                await pipeline.emit(ev)
            if isinstance(msg, AssistantMessage):
                for block in getattr(msg, "content", []) or []:
                    if isinstance(block, TextBlock):
                        self._last_assistant_text = block.text
            if isinstance(msg, ResultMessage):
                self._last_result = msg
            yield msg

    async def interrupt(self) -> None:
        await self._inner.interrupt()

    # ── pass-through control methods ---------------------------------------

    async def set_model(self, model: str | None = None) -> None:
        await self._inner.set_model(model)

    async def set_permission_mode(self, mode: Any) -> None:
        await self._inner.set_permission_mode(mode)

    async def get_context_usage(self) -> Any:
        return await self._inner.get_context_usage()

    async def get_mcp_status(self) -> Any:
        return await self._inner.get_mcp_status()

    async def get_server_info(self) -> dict[str, Any] | None:
        # Upstream's `get_server_info` is async; the proxy mirrors that.
        result: dict[str, Any] | None = await self._inner.get_server_info()
        return result

    async def reconnect_mcp_server(self, server_name: str) -> None:
        await self._inner.reconnect_mcp_server(server_name)

    async def toggle_mcp_server(self, server_name: str, enabled: bool) -> None:
        await self._inner.toggle_mcp_server(server_name, enabled)

    async def stop_task(self, task_id: str) -> None:
        await self._inner.stop_task(task_id)

    async def rewind_files(self, user_message_id: str) -> None:
        await self._inner.rewind_files(user_message_id)

    # ── internals ----------------------------------------------------------

    async def _emit_session_ended(self) -> None:
        if self._started_at is None:
            return
        duration_ms = (time.monotonic() - self._started_at) * 1000.0
        pipeline = get_pipeline()
        if self._last_result is not None:
            ev = TelemetryEvent.session_ended_ok(
                session_id=self._session_id,
                agent_name=self._agent_name,
                result=self._last_result,
                duration_ms=duration_ms,
                last_assistant_text=self._last_assistant_text,
            )
            ev.agent_description = self._agent_description
            if self._upstream_session_id is not None:
                ev.payload["upstream_session_id"] = self._upstream_session_id
            await pipeline.emit(ev)
        else:
            await pipeline.emit(
                TelemetryEvent(
                    kind="session_ended",
                    session_id=self._session_id,
                    agent_name=self._agent_name,
                    agent_description=self._agent_description,
                    payload={
                        "is_error": False,
                        "subtype": "no_result",
                        "result": self._last_assistant_text,
                        "duration_ms": duration_ms,
                        "num_turns": 0,
                        "total_cost_usd": 0.0,
                        "usage": {},
                    },
                )
            )
