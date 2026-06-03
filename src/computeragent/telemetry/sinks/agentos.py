"""``AgentRegistrySink`` — write Mongo docs that AgentOS reads.

Mirrors the TS ``@open-gitagent/agent-registry-mongo`` collections:

* ``agent_registry`` — one doc per agent name, idempotent upsert on each
  ``session_started``. First write sets ``registeredAt``; every subsequent
  write refreshes ``updatedAt`` + ``lastSeen``.
* ``agent_logs`` — one doc per run (``session_ended``), with truncated
  ``query`` / ``reply``, token + cost rollups.
* ``sessions`` — one doc per session with an ``entries[]`` array of
  ``{type, text}`` chat-bubble messages. Mirrors the harness-owned shape so
  the AgentOS SPA ``Chat`` tab can render Python-driven sessions out of the
  box. ``user_message`` and ``assistant_message`` events are appended;
  tool_use / tool_result / usage_snapshot / policy_decision are skipped
  (they have no home in the ``{type, text}`` schema and are instead
  captured by ``MongoMessageSink`` in ``agent_messages``). Opt out with
  ``AgentRegistrySink(write_sessions=False)``.

Truncation limits match TS exactly (``QUERY_MAX=8000``, ``REPLY_MAX=16000``)
so docs written from either runtime have the same shape. Uses only
DocumentDB-compatible operators (``$set``, ``$setOnInsert``, ``$push``).
"""

from __future__ import annotations

import contextlib
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
        "AgentRegistrySink requires the [agentos] extra. "
        "Install with: pip install 'computer-agent-py[agentos]'"
    ) from _exc

if TYPE_CHECKING:
    from ..event import TelemetryEvent

logger = logging.getLogger("computeragent.telemetry")


QUERY_MAX = 8_000
REPLY_MAX = 16_000


class AgentRegistrySink:
    """Write ``agent_registry`` + ``agent_logs`` docs.

    Parameters mirror the TS package's options. ``mongo_url`` accepts standard
    MongoDB URIs (``mongodb://...`` or ``mongodb+srv://...``).
    """

    def __init__(
        self,
        *,
        mongo_url: str | None = None,
        database: str | None = None,
        registry_collection: str = "agent_registry",
        logs_collection: str = "agent_logs",
        sessions_collection: str = "sessions",
        threads_collection: str = "slack_threads",
        client: Any | None = None,
        source: str = "library",
        registered_by: str | None = None,
        write_sessions: bool = True,
    ) -> None:
        self._mongo_url = mongo_url or os.environ.get("AGENTOS_MONGO_URL")
        self._database = database or os.environ.get("AGENTOS_MONGO_DB", "agentos")
        if not self._mongo_url and client is None:
            raise ValueError(
                "AgentRegistrySink requires mongo_url= (or AGENTOS_MONGO_URL env) "
                "or a client= argument"
            )
        self._client: Any = client or AsyncIOMotorClient(self._mongo_url)
        self._db = self._client[self._database]
        self._registry = self._db[registry_collection]
        self._logs = self._db[logs_collection]
        self._sessions = self._db[sessions_collection]
        # `slack_threads` is the TS aggregation table — agents.ts derives
        # sessionCount + lastActivity from it. One row per session.
        self._threads = self._db[threads_collection]
        self._source = source
        self._registered_by = registered_by or socket.gethostname()
        self._write_sessions = write_sessions

        # session_id → started timestamp + prompt (so session_ended can fill query/duration).
        self._session_meta: dict[str, dict[str, Any]] = {}
        # Sessions we've already opened in Mongo this process — avoids redundant upserts.
        self._sessions_opened: set[str] = set()

    # ── lifecycle ----------------------------------------------------------

    async def emit(self, event: TelemetryEvent) -> None:
        if event.kind == "session_started":
            await self._upsert_registry(event)
            self._session_meta[event.session_id] = {
                "started_at": event.timestamp,
                "agent_name": event.agent_name,
                "prompt": event.payload.get("prompt") or "",
                "model": event.payload.get("model"),
            }
            if self._write_sessions:
                # Track whether _open_session actually created the doc this
                # call (first time we see this session id in-process) so we
                # only seed the first user entry once per session.
                first_open = event.session_id not in self._sessions_opened
                await self._open_session(event)
                await self._open_thread(event)
                if first_open:
                    # Seed the first user entry from the prompt the agent was
                    # constructed with — matches what a human reading the
                    # SPA Chat tab expects to see as turn 1.
                    prompt_text = str(event.payload.get("prompt") or "")
                    if prompt_text:
                        await self._append_to_session(
                            event.session_id, {"type": "user", "text": prompt_text}
                        )
        elif event.kind == "session_ended":
            await self._insert_log(event)
            if self._write_sessions:
                await self._close_session(event)
                await self._close_thread(event)
            self._session_meta.pop(event.session_id, None)
            self._sessions_opened.discard(event.session_id)
        elif self._write_sessions and event.kind == "assistant_message":
            text = str(event.payload.get("text") or "")
            if text:
                await self._append_to_session(event.session_id, {"type": "assistant", "text": text})
        elif self._write_sessions and event.kind == "user_message":
            text = str(event.payload.get("text") or "")
            if text:
                await self._append_to_session(event.session_id, {"type": "user", "text": text})

    async def flush(self, timeout: float = 5.0) -> None:
        # motor flushes on each operation; nothing to do.
        return

    async def aclose(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            logger.debug("motor client close failed", exc_info=True)

    # ── writes -------------------------------------------------------------

    async def _upsert_registry(self, event: TelemetryEvent) -> None:
        name = event.agent_name or _agent_name_from_event(event)
        description = event.agent_description or ""
        now = _utcnow()
        await self._registry.update_one(
            {"_id": name},
            {
                "$setOnInsert": {
                    "_id": name,
                    "registeredAt": now,
                },
                "$set": {
                    "harness": "claude-agent-sdk",
                    # `source.manifest` carries the description too so any
                    # downstream consumer that reads the inline manifest
                    # gets the same value as the top-level `description`.
                    "source": _inline_source_for(name, event.payload, description),
                    "model": event.payload.get("model"),
                    "description": description,
                    "registeredBy": self._registered_by,
                    "updatedAt": now,
                    "lastSeen": now,
                },
            },
            upsert=True,
        )

    async def _insert_log(self, event: TelemetryEvent) -> None:
        meta = self._session_meta.get(event.session_id, {})
        usage = event.payload.get("usage") or {}
        agent_name = event.agent_name or meta.get("agent_name") or _agent_name_from_event(event)
        doc = {
            "_id": "log_" + uuid.uuid4().hex,
            "ts": _utcnow(),
            "source": self._source,
            # TS agentos-server (agent-log-store.ts) keys per-agent counts on
            # the field `bot` — a holdover from when only Slack bots wrote
            # rows. Write both so the existing UI's per-agent logCount
            # aggregation lights up for Python-driven runs.
            "bot": agent_name,
            "agentName": agent_name,
            "requester": self._registered_by or None,
            "channel": None,
            "threadTs": None,
            "sessionId": event.session_id,
            "query": _truncate(meta.get("prompt", ""), QUERY_MAX),
            "reply": _truncate(str(event.payload.get("result", "") or ""), REPLY_MAX),
            "ok": not event.payload.get("is_error", False),
        }
        if event.payload.get("is_error"):
            doc["error"] = str(
                event.payload.get("error_message") or event.payload.get("subtype") or ""
            )
        duration_ms = event.payload.get("duration_ms")
        if duration_ms is not None:
            doc["durationMs"] = int(duration_ms)
        for src_key, doc_key in (
            ("input_tokens", "inputTokens"),
            ("output_tokens", "outputTokens"),
        ):
            v = usage.get(src_key)
            if isinstance(v, int):
                doc[doc_key] = v
        cost = event.payload.get("total_cost_usd")
        if cost is not None:
            try:
                doc["costUsd"] = float(cost)
            except (TypeError, ValueError):
                doc["costUsd"] = None
        await self._logs.insert_one(doc)

    # ── sessions (Chat tab) writes ────────────────────────────────────────

    async def _open_session(self, event: TelemetryEvent) -> None:
        """Idempotent upsert of the per-session doc. The AgentOS SPA's Chat
        tab reads ``sessions.entries[]`` (normalized by ``sessions.ts``).

        Uses ``$setOnInsert`` so re-runs that reuse a session id don't reset
        the ``createdAt`` field, while ``$set`` keeps ``updatedAt`` fresh.
        """
        if event.session_id in self._sessions_opened:
            return
        agent_name = event.agent_name or self._session_meta.get(event.session_id, {}).get(
            "agent_name"
        )
        now = _utcnow()
        await self._sessions.update_one(
            {"_id": event.session_id},
            {
                "$setOnInsert": {
                    "_id": event.session_id,
                    "createdAt": now,
                    "entries": [],
                },
                "$set": {
                    "agentName": agent_name,
                    "source": self._source,
                    "model": (
                        event.payload.get("model")
                        or self._session_meta.get(event.session_id, {}).get("model")
                    ),
                    "updatedAt": now,
                },
            },
            upsert=True,
        )
        self._sessions_opened.add(event.session_id)

    async def _append_to_session(self, session_id: str, entry: dict[str, Any]) -> None:
        # Cap individual entry text to REPLY_MAX so a single huge message
        # can't push the doc over Mongo's 16MB ceiling.
        text = entry.get("text")
        if isinstance(text, str):
            entry = {**entry, "text": _truncate(text, REPLY_MAX)}
        await self._sessions.update_one(
            {"_id": session_id},
            {
                "$push": {"entries": entry},
                "$set": {"updatedAt": _utcnow()},
            },
            upsert=True,
        )

    async def _open_thread(self, event: TelemetryEvent) -> None:
        """Idempotent upsert of a ``slack_threads`` row.

        ``agentos-server/src/routes/agents.ts`` derives per-agent
        ``sessionCount`` + ``lastActivity`` from this collection (a holdover
        from when only Slack bots wrote rows). One row per session.
        """
        agent_name = event.agent_name or self._session_meta.get(event.session_id, {}).get(
            "agent_name"
        )
        if not agent_name:
            return
        now = _utcnow()
        await self._threads.update_one(
            {"_id": f"thread_{self._source}_{event.session_id}"},
            {
                "$setOnInsert": {
                    "_id": f"thread_{self._source}_{event.session_id}",
                    "createdAt": now,
                },
                "$set": {
                    "bot": agent_name,
                    "channel": self._source,
                    "threadTs": "",
                    "sessionId": event.session_id,
                    "sandboxId": None,
                    "snapshotId": None,
                    "lastMessageAt": now,
                },
            },
            upsert=True,
        )

    async def _close_thread(self, event: TelemetryEvent) -> None:
        """Stamp ``lastMessageAt`` on session end so the agent's
        ``lastActivity`` reflects the latest run."""
        await self._threads.update_one(
            {"_id": f"thread_{self._source}_{event.session_id}"},
            {"$set": {"lastMessageAt": _utcnow()}},
        )

    async def _close_session(self, event: TelemetryEvent) -> None:
        """Stamp end-of-session metadata. Doesn't append to ``entries``."""
        now = _utcnow()
        update: dict[str, Any] = {
            "endedAt": now,
            "updatedAt": now,
            "ok": not event.payload.get("is_error", False),
        }
        duration_ms = event.payload.get("duration_ms")
        if duration_ms is not None:
            with contextlib.suppress(TypeError, ValueError):
                update["durationMs"] = int(duration_ms)
        cost = event.payload.get("total_cost_usd")
        if cost is not None:
            with contextlib.suppress(TypeError, ValueError):
                update["costUsd"] = float(cost)
        await self._sessions.update_one(
            {"_id": event.session_id},
            {"$set": update},
            upsert=True,
        )


# ── helpers ────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _truncate(s: str, n: int) -> str:
    if not isinstance(s, str):
        s = str(s)
    return s if len(s) <= n else s[:n]


def _agent_name_from_event(event: TelemetryEvent) -> str:
    """Fallback agent name when none was supplied — first 32 chars of a hash."""
    import hashlib

    prompt = event.payload.get("prompt", "") if isinstance(event.payload, dict) else ""
    model = event.payload.get("model", "") if isinstance(event.payload, dict) else ""
    h = hashlib.sha256(f"{model}|{prompt}".encode()).hexdigest()[:12]
    return f"anonymous-{h}"


DEFAULT_AGENT_INSTRUCTIONS = (
    "You are a helpful assistant. Answer the user's question concisely and accurately."
)


def _inline_source_for(
    name: str,
    options_payload: dict[str, Any],
    description: str = "",
) -> dict[str, Any]:
    """Build a complete inline ``IdentitySource`` the harness can clone into a
    sandbox workdir for live chat.

    Captures the agent's ``ClaudeAgentOptions`` (system_prompt, model,
    allowed_tools, max_turns, permission_mode) as ``agent.yaml`` plus a
    ``CLAUDE.md`` so the harness's inline loader can spin up a fresh sandbox
    when the AgentOS SPA's "New Chat" button fires — matching the way
    hosted (git-sourced) agents are already supported.

    MCP server configs and the Python-side ``cwd`` are deliberately omitted —
    they don't translate across processes/filesystems. Library-mode agents
    can use built-in tools (Read, Glob, Grep) in live chat; custom MCP
    servers stay Python-only.
    """
    model = options_payload.get("model")
    system_prompt = options_payload.get("system_prompt") or DEFAULT_AGENT_INSTRUCTIONS
    allowed_tools = options_payload.get("allowed_tools") or ["Read", "Glob", "Grep"]
    max_turns = options_payload.get("max_turns")
    permission_mode = options_payload.get("permission_mode") or "bypassPermissions"

    agent_yaml = _build_agent_yaml(
        name=name,
        model=model,
        allowed_tools=allowed_tools,
        max_turns=max_turns,
        permission_mode=permission_mode,
        description=description,
    )

    manifest: dict[str, Any] = {"name": name, "version": "0.1.0"}
    if description:
        manifest["description"] = description

    return {
        "type": "inline",
        "manifest": manifest,
        "model": model,
        "files": {
            "agent.yaml": agent_yaml,
            "CLAUDE.md": system_prompt,
        },
    }


def _build_agent_yaml(
    *,
    name: str,
    model: str | None,
    allowed_tools: list[str],
    max_turns: int | None,
    permission_mode: str,
    description: str = "",
) -> str:
    """Render the agent.yaml the harness's inline loader expects.

    Minimal viable shape — name + version + model + a runtime block with the
    allowed-tools and turn limit. Matches the pattern AcmeAssist's worker
    uses in `_build_identity()` for its fallback inline source.
    """
    safe_name = _yaml_str(name)
    lines = [
        'spec_version: "0.1.0"',
        f"name: {safe_name}",
        "version: 0.1.0",
    ]
    if description:
        lines.append(f"description: {_yaml_str(description)}")
    if model:
        lines += [
            "model:",
            f"  preferred: {_yaml_str(model)}",
        ]
    lines += ["runtime:"]
    if max_turns is not None:
        lines.append(f"  max_turns: {int(max_turns)}")
    lines.append(f"  permission_mode: {_yaml_str(permission_mode)}")
    if allowed_tools:
        lines.append("  allowed_tools:")
        for t in allowed_tools:
            lines.append(f"    - {_yaml_str(t)}")
    return "\n".join(lines) + "\n"


def _yaml_str(s: str) -> str:
    """Single-line scalar safe for YAML. Quotes everything to avoid the
    headache of indicator characters (``:``, ``-``, ``*``, ``#``, …) in
    agent names / system prompts."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
