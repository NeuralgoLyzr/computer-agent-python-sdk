"""``AgentOSHttpSink`` — POST telemetry events to the AgentOS ingest endpoint.

Replaces the in-process Mongo sinks (``AgentRegistrySink`` + ``MongoMessageSink``).
The AgentOS server now owns the Mongo document schema: this sink just serializes
each :class:`~computeragent.telemetry.event.TelemetryEvent` to a stable wire DTO
and POSTs batches to ``POST /agentos/api/ingest/events``. The server projects
each event into ``agent_registry`` / ``agent_logs`` / ``sessions`` /
``chat_sessions`` / ``agent_messages``.

Why HTTP instead of Mongo:

* Python deployments no longer need Mongo creds or the ``motor`` driver.
* The dashboard's document schema can evolve without a Python release.
* Writes land in the server's database, so there's no ``AGENTOS_MONGO_DB``
  vs ``MONGO_DATABASE`` mismatch.

Batching: ``emit`` appends to an in-order buffer (synchronously, before any
``await``, so buffer order matches emit order). The buffer is drained — POSTed —
on a size/byte threshold, on every ``session_ended`` (so each run lands
promptly), and on ``flush()`` / ``aclose()``. A failed POST drops the batch and
logs; it never raises into the agent run (matches the sink contract).
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

try:
    import httpx
except ImportError as _exc:  # pragma: no cover - guarded by lazy export
    raise ImportError(
        "AgentOSHttpSink requires the [agentos] extra. "
        "Install with: pip install 'computer-agent-py[agentos]'"
    ) from _exc

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..event import TelemetryEvent

logger = logging.getLogger("computeragent.telemetry")

# Drain in small batches so a single POST stays well under the pipeline's
# default 5 s flush window — the server fans each event out to a remote
# Mongo/DocumentDB (one round-trip per write), and the harness flushes telemetry
# with a 5 s budget on exit. Keeping batches small means the end-of-session
# drain (which flushes whatever remains) completes inside that window even
# without an explicit flush, so the final turn isn't lost on a fast exit.
DEFAULT_FLUSH_THRESHOLD = 10
# Stay clear of the server's 5 MB body cap.
DEFAULT_MAX_BUFFER_BYTES = 4_000_000
# session_ended forces a drain so each run's rollup + transcript land promptly.
_DRAIN_KINDS = {"session_ended"}


class AgentOSHttpSink:
    """Buffer telemetry events and POST them to the AgentOS ingest endpoint."""

    def __init__(
        self,
        *,
        url: str | None = None,
        token: str | None = None,
        source: str = "library",
        host: str | None = None,
        flush_threshold: int = DEFAULT_FLUSH_THRESHOLD,
        max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
        client: Any | None = None,
        timeout: float = 30.0,
        api_key: Callable[[], str] | None = None,
    ) -> None:
        from ... import agentos as _agentos

        self._url = url or _agentos.ingest_url()
        if not self._url:
            raise ValueError(
                "AgentOSHttpSink requires url= (or AGENTOS_DISCOVERY_URL / the "
                "AGENTOS_INGEST_URL override env var)"
            )
        # The one cak_ key the SDK presents everywhere is the ingest bearer; a
        # legacy AGENTOS_INGEST_TOKEN string is still honoured via agentos.cak().
        # `api_key`, when given, is read at every POST and sent as x-api-key
        # (the egress proxy swaps it); no bearer token is sent then.
        self._api_key = api_key
        self._token = token or (None if api_key is not None else _agentos.cak())
        self._source = source
        self._host = host or socket.gethostname()
        self._flush_threshold = max(1, flush_threshold)
        self._max_buffer_bytes = max_buffer_bytes
        headers = {"content-type": "application/json"}
        if self._token:
            headers["authorization"] = f"Bearer {self._token}"
        self._client: Any = client or httpx.AsyncClient(timeout=timeout, headers=headers)
        self._owns_client = client is None
        self._buffer: list[dict[str, Any]] = []
        self._buffer_bytes = 0
        self._lock = asyncio.Lock()

    # ── lifecycle ----------------------------------------------------------

    async def emit(self, event: TelemetryEvent) -> None:
        # response_first_token is an OTel-trace-only signal (it drives the TTFT
        # span attribute in OtelSink). It carries no audit value, so it must NOT
        # be projected into Mongo via the ingest endpoint.
        if event.kind == "response_first_token":
            return
        dto = self._serialize(event)
        # Append synchronously (no await before this) so the buffer preserves
        # the order in which the pipeline scheduled the emit tasks.
        self._buffer.append(dto)
        self._buffer_bytes += len(json.dumps(dto, default=str))
        if (
            event.kind in _DRAIN_KINDS
            or len(self._buffer) >= self._flush_threshold
            or self._buffer_bytes >= self._max_buffer_bytes
        ):
            await self._drain()

    async def flush(self, timeout: float = 5.0) -> None:
        await self._drain()

    async def aclose(self) -> None:
        try:
            await self._drain()
        finally:
            if self._owns_client:
                try:
                    await self._client.aclose()
                except Exception:  # noqa: BLE001
                    logger.debug("httpx client close failed", exc_info=True)

    # ── internals ----------------------------------------------------------

    def _serialize(self, event: TelemetryEvent) -> dict[str, Any]:
        ts = event.timestamp
        return {
            # Stable id → the server keys agent_messages / agent_logs on it, so
            # a retried batch is idempotent rather than duplicating rows.
            "event_id": uuid.uuid4().hex,
            "kind": event.kind,
            "session_id": event.session_id,
            "agent_id": event.agent_id,
            "timestamp": ts.isoformat() if isinstance(ts, datetime) else str(ts),
            "agent_name": event.agent_name,
            "agent_description": event.agent_description,
            # Process-fork linkage — present only for children spawned via
            # mcp__fork__spawn; None otherwise. Lets the server nest child
            # sessions under the parent (rendering is a follow-up).
            "parent_session_id": event.parent_session_id,
            "parent_agent_id": event.parent_agent_id,
            "host": self._host,
            "payload": event.payload or {},
        }

    async def _drain(self) -> None:
        async with self._lock:
            if not self._buffer:
                return
            batch = self._buffer
            self._buffer = []
            self._buffer_bytes = 0
            try:
                # default=str coerces datetimes / non-JSON values the same way
                # the Mongo sink relied on bson to handle them.
                body = json.dumps({"events": batch}, default=str)
                headers = {"x-api-key": self._api_key()} if self._api_key is not None else None
                resp = await self._client.post(self._url, content=body, headers=headers)
                resp.raise_for_status()
            except Exception:  # noqa: BLE001 - sinks must never break the agent
                # Drop the batch (don't requeue — a poisoned batch would wedge
                # the buffer forever). Telemetry loss is acceptable; a crash isn't.
                logger.warning(
                    "AgentOSHttpSink POST failed; dropping %d event(s)",
                    len(batch),
                    exc_info=True,
                )
