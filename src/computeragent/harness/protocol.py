"""Harness Protocol — wire types + an async client for a remote harness server.

Python port of the TypeScript ``@open-gitagent/sdk`` SSE+POST client. Speaks the
``/v1/sessions/*`` protocol that ``@computeragent/harness-server`` serves
(``examples/harness-server.ts``, default port 7700). Usable standalone, or via
``ComputerAgent(harness_url=...)``.

The ``HarnessEvent`` dataclasses + :func:`parse_sse` here are **shared** with the
ComputerAgent-server client (``cas_client.py``) — both surfaces emit the same
``ca_*`` event union over SSE.
"""

from __future__ import annotations

import codecs
import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, cast

try:
    import httpx
except ImportError as _exc:  # pragma: no cover - guarded by lazy export
    raise ImportError(
        "The remote harness client requires the [remote] extra. "
        "Install with: pip install 'computer-agent-py[remote]'"
    ) from _exc

from .errors import HarnessProtocolError

# ── HarnessEvent dataclasses ─────────────────────────────────────────────────
# Mirror packages/protocol/src/sse-events.ts. Wire fields are camelCase; these
# carry snake_case attrs (the QA worker reads `event.session_id`, `.reason`,
# `.error_message`). `parse_event` maps camelCase → snake.


@dataclass(frozen=True)
class HarnessEvent:
    kind: str
    session_id: str = ""


@dataclass(frozen=True)
class SdkMessage(HarnessEvent):
    """Engine-native message forwarded verbatim (claude-agent-sdk SDKMessage dict)."""

    payload: Any = None


@dataclass(frozen=True)
class CaSessionStarted(HarnessEvent):
    engine: str = ""
    identity: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CaTurnStarted(HarnessEvent):
    turn_index: int = 0
    message: dict[str, Any] | None = None


@dataclass(frozen=True)
class CaPermissionRequest(HarnessEvent):
    call_id: str = ""
    tool_name: str = ""
    input: Any = None
    risk: str | None = None


@dataclass(frozen=True)
class CaPermissionDecision(HarnessEvent):
    call_id: str = ""
    decision: str = "allow"
    reason: str | None = None


@dataclass(frozen=True)
class CaUsageSnapshot(HarnessEvent):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cost_usd: float | None = None
    cost_semantic: str | None = None


@dataclass(frozen=True)
class CaSessionEnded(HarnessEvent):
    reason: str = "complete"
    error_message: str | None = None


@dataclass(frozen=True)
class CaDone(HarnessEvent):
    """ComputerAgent-server (CAS) framing — terminal turn marker with the usage rollup.
    Not part of the bare ``/v1`` protocol union; emitted by ``/run`` and
    ``/sandboxes/:id/chat``."""

    usage: dict[str, Any] = field(default_factory=dict)
    sandbox_id: str | None = None
    turn: int | None = None


@dataclass(frozen=True)
class CaError(HarnessEvent):
    """CAS framing — a turn-level error (no `sessionId` on the wire)."""

    message: str = ""


@dataclass(frozen=True)
class UnknownEvent(HarnessEvent):
    """Forward-compat: a kind this client version doesn't model yet."""

    raw: dict[str, Any] = field(default_factory=dict)


def parse_event(raw: Mapping[str, Any]) -> HarnessEvent:
    """Map a decoded SSE ``data:`` JSON object to a ``HarnessEvent`` dataclass.

    Unknown kinds become :class:`UnknownEvent` so a newer server never breaks
    the client.
    """
    kind = str(raw.get("kind", ""))
    sid = str(raw.get("sessionId") or raw.get("session_id") or "")
    if kind == "sdk_message":
        return SdkMessage(kind, sid, payload=raw.get("payload"))
    if kind == "ca_session_started":
        return CaSessionStarted(
            kind, sid,
            engine=str(raw.get("engine", "")),
            identity=dict(raw.get("identity") or {}),
            capabilities=dict(raw.get("capabilities") or {}),
        )
    if kind == "ca_turn_started":
        return CaTurnStarted(kind, sid, turn_index=int(raw.get("turnIndex", 0) or 0), message=raw.get("message"))
    if kind == "ca_permission_request":
        return CaPermissionRequest(
            kind, sid,
            call_id=str(raw.get("callId", "")),
            tool_name=str(raw.get("toolName", "")),
            input=raw.get("input"),
            risk=raw.get("risk"),
        )
    if kind == "ca_permission_decision":
        return CaPermissionDecision(
            kind, sid, call_id=str(raw.get("callId", "")), decision=str(raw.get("decision", "allow")), reason=raw.get("reason")
        )
    if kind == "ca_usage_snapshot":
        return CaUsageSnapshot(
            kind, sid,
            input_tokens=raw.get("inputTokens"),
            output_tokens=raw.get("outputTokens"),
            cache_creation_input_tokens=raw.get("cacheCreationInputTokens"),
            cache_read_input_tokens=raw.get("cacheReadInputTokens"),
            cost_usd=raw.get("costUsd"),
            cost_semantic=raw.get("costSemantic"),
        )
    if kind == "ca_session_ended":
        return CaSessionEnded(kind, sid, reason=str(raw.get("reason", "complete")), error_message=raw.get("errorMessage"))
    if kind == "ca_done":
        return CaDone(kind, sid, usage=dict(raw.get("usage") or {}), sandbox_id=raw.get("sandboxId"), turn=raw.get("turn"))
    if kind == "ca_error":
        return CaError(kind, sid, message=str(raw.get("message", "")))
    return UnknownEvent(kind, sid, raw=dict(raw))


# ── SSE parser (port of packages/sdk/src/sse-client.ts consumeSseEvents) ─────


async def parse_sse(byte_iter: AsyncIterator[bytes]) -> AsyncIterator[tuple[int | None, dict[str, Any]]]:
    """Consume an SSE byte stream → yield ``(id, data-object)`` per event block.

    Tolerant of partial chunks (incremental UTF-8 decode), multi-line ``data:``,
    ``id:`` tracking, and ``:keepalive`` comment lines.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    buf = ""
    async for chunk in byte_iter:
        buf += decoder.decode(chunk)
        while "\n\n" in buf:
            block, buf = buf.split("\n\n", 1)
            parsed = _parse_block(block)
            if parsed is not None:
                yield parsed
    buf += decoder.decode(b"", final=True)
    if buf.strip():
        parsed = _parse_block(buf)
        if parsed is not None:
            yield parsed


def _parse_block(block: str) -> tuple[int | None, dict[str, Any]] | None:
    if not block.strip():
        return None
    data_lines: list[str] = []
    event_id: int | None = None
    for raw_line in block.split("\n"):
        line = raw_line.rstrip("\r")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line.startswith("id:"):
            with suppress(ValueError):
                event_id = int(line[3:].strip())
        # ignore "event:" lines and ":" comment / keepalive lines
    if not data_lines:
        return None
    try:
        obj = json.loads("\n".join(data_lines))
    except (ValueError, TypeError):
        return None
    if isinstance(obj, dict) and "kind" in obj:
        return (event_id, obj)
    return None


# ── helpers ──────────────────────────────────────────────────────────────────


def _resolve_basic_auth(auth: tuple[str, str] | str | None) -> tuple[str, str] | None:
    """Basic-auth tuple from explicit arg or the harness-server's API_AUTH_* envs."""
    if isinstance(auth, tuple):
        return auth
    if auth is None:
        u, p = os.environ.get("API_AUTH_USER"), os.environ.get("API_AUTH_PASS")
        if u and p:
            return (u, p)
    return None


def user_message(content: Any) -> dict[str, Any]:
    """Normalize a chat input into the wire ``UserMessage`` shape."""
    if isinstance(content, dict) and content.get("role"):
        return content
    if isinstance(content, dict):
        return {"role": "user", "content": content.get("content", content)}
    return {"role": "user", "content": content}


# ── HarnessClient — the bare /v1/sessions/* protocol ─────────────────────────


class HarnessClient:
    """Async client for the bare harness protocol (`/v1/sessions/*`)."""

    def __init__(
        self,
        *,
        base_url: str,
        auth: tuple[str, str] | str | None = None,
        token: str | None = None,
        timeout: float | None = None,
        client: Any | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        headers = {"content-type": "application/json"}
        bearer = token or (auth if isinstance(auth, str) else None)
        if bearer:
            headers["authorization"] = f"Bearer {bearer}"
        # Only fall back to Basic when no bearer was supplied (avoid double auth).
        basic = None if bearer else _resolve_basic_auth(auth)
        # read=None → no read timeout, so long SSE poll + slow tool calls don't time out.
        to = httpx.Timeout(timeout if timeout is not None else 30.0, read=None)
        self._client: Any = client or httpx.AsyncClient(timeout=to, headers=headers, auth=basic)
        self._owns_client = client is None

    # ── REST ──────────────────────────────────────────────────────────────

    async def create_session(self, body: dict[str, Any]) -> dict[str, Any]:
        r = await self._client.post(f"{self._base}/v1/sessions", json=body)
        await self._raise_for_status(r, "create_session")
        return cast("dict[str, Any]", r.json())

    async def send_message(self, session_id: str, message: dict[str, Any]) -> None:
        r = await self._client.post(
            f"{self._base}/v1/sessions/{session_id}/messages", json={"message": message}
        )
        await self._raise_for_status(r, "send_message")

    async def decide_permission(
        self, session_id: str, call_id: str, decision: str, *, input: Any = None, reason: str | None = None
    ) -> None:
        body: dict[str, Any] = {"decision": decision}
        if input is not None:
            body["input"] = input
        if reason is not None:
            body["reason"] = reason
        r = await self._client.post(
            f"{self._base}/v1/sessions/{session_id}/permission/{call_id}", json=body
        )
        await self._raise_for_status(r, "decide_permission")

    async def end_input(self, session_id: str) -> None:
        r = await self._client.post(f"{self._base}/v1/sessions/{session_id}/end-input")
        await self._raise_for_status(r, "end_input")

    async def delete_session(self, session_id: str) -> None:
        r = await self._client.delete(f"{self._base}/v1/sessions/{session_id}")
        await self._raise_for_status(r, "delete_session")

    # ── SSE ─────────────────────────────────────────────────────────────────

    async def stream_events(
        self, session_id: str, last_event_id: int | None = None
    ) -> AsyncIterator[tuple[int | None, HarnessEvent]]:
        """Yield ``(event_id, HarnessEvent)`` from the session's SSE stream.
        Pass ``last_event_id`` to resume (server replays events after it)."""
        headers: dict[str, str] = {}
        if last_event_id is not None and last_event_id >= 0:
            headers["last-event-id"] = str(last_event_id)
        async with self._client.stream(
            "GET", f"{self._base}/v1/sessions/{session_id}/events", headers=headers
        ) as r:
            await self._raise_for_status(r, "stream_events", streaming=True)
            async for event_id, raw in parse_sse(r.aiter_bytes()):
                yield event_id, parse_event(raw)

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._owns_client:
            with suppress(Exception):
                await self._client.aclose()

    async def _raise_for_status(self, r: Any, where: str, *, streaming: bool = False) -> None:
        if r.status_code < 400:
            return
        body = ""
        with suppress(Exception):
            body = (await r.aread()).decode("utf-8", "replace") if streaming else r.text
        raise HarnessProtocolError(f"{where}: HTTP {r.status_code} from {self._base} — {body[:500]}")


__all__ = [
    "CaDone",
    "CaError",
    "CaPermissionDecision",
    "CaPermissionRequest",
    "CaSessionEnded",
    "CaSessionStarted",
    "CaTurnStarted",
    "CaUsageSnapshot",
    "HarnessClient",
    "HarnessEvent",
    "SdkMessage",
    "UnknownEvent",
    "parse_event",
    "parse_sse",
    "user_message",
]
