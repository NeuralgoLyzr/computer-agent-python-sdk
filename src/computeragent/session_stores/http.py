"""``HttpSessionStore`` — transcripts over HTTP, with the server doing the write.

Port of the TS ``@open-gitagent/session-store-http``. Two verbs against the
session-store service, which performs the database write::

    GET  {base}/session-store/entries?sessionId=&projectKey=  -> {entries}
    POST {base}/session-store/entries  {sessionId, projectKey, entries}

It exists so a harness that cannot reach a VPC-private database — and must not
hold its credential — can still persist transcripts. Behind the egress proxy
``base`` is ``http://egress/computer-agent-api``; the only credential sent is
``x-api-key`` (the newest sandbox token), which the proxy swaps for the Lyzr
key, adding the session it scopes entries to.

Only ``append`` and ``load`` are implemented, and the omissions are deliberate:
see the note on :meth:`load` and the class docstring's tail.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from ..protocol_types import SessionKey, SessionStoreEntry

logger = logging.getLogger("computeragent.session_stores.http")

#: Appends are frequent and off the turn's critical path; the SDK retries behind
#: us and gives up at 60s.
DEFAULT_APPEND_TIMEOUT = 10.0
#: Deliberately looser than append. `load` runs once per session and a failure
#: resolves to None — indistinguishable from "new session" — so the whole prior
#: transcript is silently dropped. Being impatient here trades a rare slow
#: session start for an agent that quietly forgot the conversation.
DEFAULT_LOAD_TIMEOUT = 15.0


class HttpSessionStore:
    kind = "http"

    def __init__(
        self,
        *,
        base_url: str,
        append_timeout: float = DEFAULT_APPEND_TIMEOUT,
        load_timeout: float = DEFAULT_LOAD_TIMEOUT,
        api_key: Callable[[], str | None] | None = None,
        client: Any | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("HttpSessionStore: base_url is required")
        self._base = base_url.rstrip("/")
        self._append_timeout = append_timeout
        self._load_timeout = load_timeout
        self._api_key = api_key or _env_api_key

        from ..harness.egress_token import egress_event_hooks, install

        if client is not None:
            self._client = install(client)
            self._owns_client = False
        else:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - guarded by the extra
                raise ImportError(
                    "HttpSessionStore requires httpx. "
                    "Install with: pip install 'computer-agent-py[agentos]'"
                ) from exc
            self._client = httpx.AsyncClient(event_hooks=egress_event_hooks(True))
            self._owns_client = True

        # The contract makes ordering ours to keep — "within a single process,
        # persist entries in append-call order" — and the SDK may fire the next
        # batch without awaiting the previous. Two concurrent HTTP appends can
        # reorder in flight and race the server's read-then-write dedup, so a
        # lock removes the concurrency at the source.
        self._append_lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        """``x-api-key`` when a key is set; none for an unauthenticated local store."""
        key = self._api_key()
        return {"x-api-key": key} if key else {}

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        """Load a session for resume. **Never raises.**

        Every failure — network, timeout, auth, 5xx — resolves to ``None``,
        which the contract already defines as "never written". The engine awaits
        this at session start, so an unguarded raise turns a dead store into a
        dead session. The cost of that safety is silence, so every failure is
        logged with its elapsed time: that line is the only signal an operator
        gets that an agent is about to start with no memory.
        """
        started = time.monotonic()
        params = {"sessionId": key["session_id"], "projectKey": key["project_key"]}
        if key.get("subpath"):
            params["subpath"] = key["subpath"]
        try:
            resp = await self._client.get(
                f"{self._base}/session-store/entries",
                params=params,
                headers=self._headers(),
                timeout=self._load_timeout,
            )
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                self._warn_load(key, started, f"HTTP {resp.status_code}")
                return None
            entries = resp.json().get("entries")
            if not isinstance(entries, list) or not entries:
                return None
            return entries
        except Exception as exc:  # noqa: BLE001 — see docstring: never raises
            self._warn_load(key, started, f"{type(exc).__name__}: {exc}")
            return None

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        """Mirror a batch.

        Retries ONCE on a transient failure and never on a permanent one. The
        SDK already retries three times; matching that here would mean up to
        nine attempts and a multi-minute tail on a store that is simply down.
        A 4xx is the server saying the request itself is wrong — retrying
        cannot help.
        """
        if not entries:
            return
        body: dict[str, Any] = {
            "sessionId": key["session_id"],
            "projectKey": key["project_key"],
            "entries": entries,
        }
        if key.get("subpath"):
            body["subpath"] = key["subpath"]

        async with self._append_lock:
            last: Exception | None = None
            for _attempt in range(2):
                try:
                    resp = await self._client.post(
                        f"{self._base}/session-store/entries",
                        json=body,
                        headers={**self._headers(), "content-type": "application/json"},
                        timeout=self._append_timeout,
                    )
                    if resp.status_code < 400:
                        with contextlib.suppress(Exception):
                            if resp.json().get("truncated"):
                                # The server shortened an entry rather than
                                # rejecting the batch — better than a hole in
                                # the transcript, but it changes what the model
                                # sees on resume, so it must not be silent.
                                logger.warning(
                                    "session store truncated entries for session %s",
                                    key["session_id"],
                                )
                        return
                    if 400 <= resp.status_code < 500:
                        msg = f"session store rejected append: HTTP {resp.status_code}"
                        logger.warning("%s (permanent, not retried)", msg)
                        raise SessionStoreAppendError(msg)
                    last = SessionStoreAppendError(
                        f"session store append failed: HTTP {resp.status_code}"
                    )
                except SessionStoreAppendError:
                    raise
                except Exception as exc:  # noqa: BLE001 — retried once, then raised
                    last = exc
            logger.warning("session store append failed for %s: %s", key["session_id"], last)
            raise last if last else SessionStoreAppendError("session store append failed")

    # `list_sessions`, `delete` and `list_subkeys` are deliberately ABSENT, not
    # stubbed. `list_sessions` is scoped to a whole project and cannot be
    # narrowed to one session without becoming meaningless, so exposing it would
    # hand one sandbox a listing of every other session; the others have no
    # caller and no authorization story. The SDK probes for these methods, so
    # absence is the correct way to say "this backend does not do that" — a
    # well-meaning stub would change behaviour.

    async def close(self) -> None:
        """Close the client this store opened. Idempotent."""
        if self._owns_client:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._owns_client = False

    def _warn_load(self, key: SessionKey, started: float, reason: str) -> None:
        logger.warning(
            "session store load failed for %s (%s) after %.0fms — "
            "resuming without prior transcript",
            key["session_id"],
            reason,
            (time.monotonic() - started) * 1000,
        )


class SessionStoreAppendError(RuntimeError):
    """An append the server refused, or that failed twice in a row."""


def _env_api_key() -> str | None:
    """``COMPUTER_AGENT_EGRESS_PROXY_SECRET`` as it stands now, as the newest token of its chain."""
    from ..harness.egress_token import CREDENTIAL

    value = os.environ.get("COMPUTER_AGENT_EGRESS_PROXY_SECRET", "").strip()
    return CREDENTIAL.resolve(value) if value else None


def http_session_store_builder(**defaults: Any) -> Callable[[Any], HttpSessionStore]:
    """Registry builder.

    Takes no connection defaults: ``base_url`` comes per session (from
    ``COMPUTER_AGENT_EGRESS_SESSION_STORE_URL`` through :func:`with_base_url`, or the
    descriptor). A ``token`` option, which older dispatchers still send, is
    dropped: the store sends only ``x-api-key``.
    """

    def _build(options: Any = None) -> HttpSessionStore:
        merged = {**defaults, **(dict(options) if isinstance(options, Mapping) else {})}
        merged.pop("token", None)
        merged.pop("baseUrl", None)
        merged.pop("auto_refresh", None)
        if not merged.get("base_url"):
            raise ValueError(
                "http session store: options.base_url is required — set "
                "COMPUTER_AGENT_EGRESS_SESSION_STORE_URL or send it in the descriptor"
            )
        return HttpSessionStore(**merged)

    return _build


def with_base_url(spec: Any, url: str | None) -> Any:
    """Apply ``COMPUTER_AGENT_EGRESS_SESSION_STORE_URL`` to a session-store spec.

    A store instance, or a kind other than ``http``, is returned unchanged.
    For kind ``http`` with ``url`` set, ``options.base_url`` becomes ``url``
    and any ``base_url``/``baseUrl``/``token`` in the descriptor is dropped.
    Without ``url`` the spec is returned unchanged.
    """
    if url is None:
        return spec
    if isinstance(spec, str):
        if spec != "http":
            logger.debug("session store URL setting ignored for this kind")
            return spec
        options: dict[str, Any] = {}
    elif isinstance(spec, Mapping) and spec.get("kind") == "http":
        raw = spec.get("options")
        options = dict(raw) if isinstance(raw, Mapping) else {}
        for name in ("base_url", "baseUrl", "token"):
            options.pop(name, None)
    else:
        logger.debug("session store URL setting ignored for this kind")
        return spec
    logger.info("session store base URL taken from COMPUTER_AGENT_EGRESS_SESSION_STORE_URL")
    return {"kind": "http", "options": {**options, "base_url": url}}


__all__ = [
    "DEFAULT_APPEND_TIMEOUT",
    "DEFAULT_LOAD_TIMEOUT",
    "HttpSessionStore",
    "SessionStoreAppendError",
    "http_session_store_builder",
    "with_base_url",
]
