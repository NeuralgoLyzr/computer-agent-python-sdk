"""``MongoMessageSink`` — per-message archive in Mongo.

Writes one document per non-rollup telemetry event into the ``agent_messages``
collection (parallel to ``agent_registry`` + ``agent_logs`` written by
:class:`AgentRegistrySink`). Sits in the ``[agentos]`` extra alongside
``motor``, so installing AgentOS support enables both sinks.

Each document carries:

* ``sessionId`` / ``agentName`` — primary indexes for transcript queries
* ``kind`` — the :class:`EventKind` (``user_message``, ``assistant_message``,
  ``tool_use``, ``tool_result``, ``usage_snapshot``, ``policy_decision``,
  ``system_message``)
* ``payload`` — the post-middleware payload (PII-redacted by default; that's
  the standard pipeline ordering — middleware runs before sinks)
* ``ts`` — emission time
* ``latencyMs`` — only present on ``policy_decision`` today

``session_started`` and ``session_ended`` are skipped: those rollup events
already land in ``agent_logs`` via :class:`AgentRegistrySink`. Writing them
here too would just duplicate the registry contract.

Use case: per-session transcript replay in the AgentOS frontend, RAG ingestion
over historical agent runs, audit / forensics — all of which need the full
message stream rather than the session-rollup view in ``agent_logs``.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

try:
    from motor.motor_asyncio import AsyncIOMotorClient
except ImportError as _exc:  # pragma: no cover - guarded by lazy export
    raise ImportError(
        "MongoMessageSink requires the [agentos] extra. "
        "Install with: pip install 'computer-agent-py[agentos]'"
    ) from _exc

if TYPE_CHECKING:
    from ..event import TelemetryEvent

logger = logging.getLogger("computeragent.telemetry")

# Per-field truncation caps. Mongo's hard ceiling is 16 MB per doc; these
# defaults keep us well clear while still letting most real prompts /
# responses land verbatim.
DEFAULT_MAX_FIELD_CHARS = 32_000

# Kinds that the AgentRegistrySink already covers via agent_logs.
_ROLLUP_KINDS = {"session_started", "session_ended"}


class MongoMessageSink:
    """Per-message Mongo archive. Pairs with :class:`AgentRegistrySink`."""

    def __init__(
        self,
        *,
        mongo_url: str | None = None,
        database: str | None = None,
        collection: str = "agent_messages",
        client: Any | None = None,
        max_field_chars: int = DEFAULT_MAX_FIELD_CHARS,
        source: str = "library",
        host: str | None = None,
    ) -> None:
        self._mongo_url = mongo_url or os.environ.get("AGENTOS_MONGO_URL")
        self._database = database or os.environ.get("AGENTOS_MONGO_DB", "agentos")
        if not self._mongo_url and client is None:
            raise ValueError(
                "MongoMessageSink requires mongo_url= (or AGENTOS_MONGO_URL env) "
                "or a client= argument"
            )
        self._client: Any = client or AsyncIOMotorClient(self._mongo_url)
        self._coll = self._client[self._database][collection]
        self._max_field_chars = max_field_chars
        self._source = source
        self._host = host or socket.gethostname()

    async def emit(self, event: TelemetryEvent) -> None:
        if event.kind in _ROLLUP_KINDS:
            return  # AgentRegistrySink owns these
        doc = {
            "_id": "msg_" + uuid.uuid4().hex,
            "ts": event.timestamp or _utcnow(),
            "source": self._source,
            "host": self._host,
            "sessionId": event.session_id,
            "agentName": event.agent_name,
            "kind": event.kind,
            "payload": _truncate(event.payload, self._max_field_chars),
        }
        # Surface latency at the top level for the few event kinds that carry
        # it — makes per-message latency dashboards trivial in Mongo / Atlas.
        latency = (event.payload or {}).get("latency_ms")
        if isinstance(latency, (int, float)):
            doc["latencyMs"] = float(latency)
        await self._coll.insert_one(doc)

    async def flush(self, timeout: float = 5.0) -> None:
        # motor flushes per operation; nothing to do.
        return

    async def aclose(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            logger.debug("motor client close failed", exc_info=True)


# ── helpers ────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _truncate(value: object, limit: int) -> object:
    """Walk a payload, truncating any string longer than `limit` chars.

    Mirrors GuardrailFilter's truncation shape — string + a footer noting how
    many chars were dropped, so consumers see what was elided.
    """
    if isinstance(value, dict):
        return {k: _truncate(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v, limit) for v in value]
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"…(+{len(value) - limit} chars)"
    return value
