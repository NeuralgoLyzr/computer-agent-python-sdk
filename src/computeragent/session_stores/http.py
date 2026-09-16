"""``HttpSessionStore`` — transcripts over HTTP, with the server doing the write.

Port of the TS ``@open-gitagent/session-store-http``. Two authenticated verbs
against the ComputerAgent server, which performs the database write in-pod::

    GET  {base}/session-store/entries?sessionId=&projectKey=  -> {entries}
    POST {base}/session-store/entries  {sessionId, projectKey, entries}

It exists so a harness that cannot reach a VPC-private database — and must not
hold its credential — can still persist transcripts. Requires the ``[agentos]``
or ``[remote]`` extra for ``httpx``.

Only ``append`` and ``load`` are implemented, and the omissions are deliberate:
see the note on :meth:`load` and the class docstring's tail.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import time
from typing import Any

from ..protocol_types import SessionKey, SessionStoreEntry

logger = logging.getLogger("computeragent.session_stores.http")

#: The token travels on its own header, never ``Authorization`` or
#: ``x-api-key``. This is structural, not stylistic: the same listener hosts the
#: Anthropic-to-OpenAI translator and the MCP bridge, and BOTH read the caller's
#: credential from exactly those two headers and forward it verbatim to a third
#: party. A dedicated header makes handing this signed token to an external
#: service impossible rather than merely unlikely.
TOKEN_HEADER = "x-ca-session-store-token"

#: Appends are frequent and off the turn's critical path; the SDK retries behind
#: us and gives up at 60s.
DEFAULT_APPEND_TIMEOUT = 10.0
#: Deliberately looser than append. `load` runs once per session and a failure
#: resolves to None — indistinguishable from "new session" — so the whole prior
#: transcript is silently dropped. Being impatient here trades a rare slow
#: session start for an agent that quietly forgot the conversation.
DEFAULT_LOAD_TIMEOUT = 15.0

#: Refresh at this fraction of remaining lifetime (kubelet's ratio).
REFRESH_AT_FRACTION = 0.8
MIN_REFRESH_DELAY = 5.0


def expiry_of(token: str) -> int | None:
    """Read ``exp`` out of a JWT **without verifying it**.

    Unverified on purpose: this client is not the verifier — the server is, and
    it holds the only key. All that is needed is a hint for when to refresh, and
    a wrong hint costs at most one wasted call, because the server still refuses
    anything invalid. ``None`` for an opaque or malformed token, in which case
    no refresh is scheduled at all.
    """
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    exp = payload.get("exp")
    return exp if isinstance(exp, int) else None


class HttpSessionStore:
    kind = "http"

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        append_timeout: float = DEFAULT_APPEND_TIMEOUT,
        load_timeout: float = DEFAULT_LOAD_TIMEOUT,
        auto_refresh: bool = True,
        client: Any | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("HttpSessionStore: base_url is required")
        if not token:
            raise ValueError("HttpSessionStore: token is required")
        self._base = base_url.rstrip("/")
        self._token = token
        self._append_timeout = append_timeout
        self._load_timeout = load_timeout
        self._auto_refresh = auto_refresh
        self._refresh_task: asyncio.Task[None] | None = None

        if client is not None:
            self._client = client
        else:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - guarded by the extra
                raise ImportError(
                    "HttpSessionStore requires httpx. "
                    "Install with: pip install 'computer-agent-py[agentos]'"
                ) from exc
            self._client = httpx.AsyncClient()

        # The contract makes ordering ours to keep — "within a single process,
        # persist entries in append-call order" — and the SDK may fire the next
        # batch without awaiting the previous. Two concurrent HTTP appends can
        # reorder in flight and race the server's read-then-write dedup, so a
        # lock removes the concurrency at the source.
        self._append_lock = asyncio.Lock()

    @property
    def token(self) -> str:
        return self._token

    def _headers(self) -> dict[str, str]:
        return {TOKEN_HEADER: self._token}

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        """Load a session for resume. **Never raises.**

        Every failure — network, timeout, auth, 5xx — resolves to ``None``,
        which the contract already defines as "never written". The engine awaits
        this at session start, so an unguarded raise turns a dead store into a
        dead session. The cost of that safety is silence, so every failure is
        logged with its elapsed time: that line is the only signal an operator
        gets that an agent is about to start with no memory.
        """
        self._arm_refresh()
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
        self._arm_refresh()
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

    async def refresh_token(self) -> bool:
        """Exchange the current token for a fresh one. Failure is non-fatal —
        the existing token stays in use and the server mints a new one at the
        next dispatch anyway."""
        try:
            resp = await self._client.post(
                f"{self._base}/v1/auth/token",
                json={},
                headers={**self._headers(), "content-type": "application/json"},
                timeout=self._append_timeout,
            )
            if resp.status_code >= 400:
                logger.warning("session store token refresh failed: HTTP %s", resp.status_code)
                return False
            token = resp.json().get("token")
            if isinstance(token, str) and token:
                self._token = token
                return True
        except Exception as exc:  # noqa: BLE001 — never fatal
            logger.warning("session store token refresh failed: %s", exc)
        return False

    def _arm_refresh(self) -> None:
        if not self._auto_refresh or self._refresh_task is not None:
            return
        exp = expiry_of(self._token)
        if exp is None:
            return  # opaque token — nothing to schedule against
        remaining = exp - time.time()
        if remaining <= 0:
            return
        delay = max(MIN_REFRESH_DELAY, remaining * REFRESH_AT_FRACTION)

        async def _later() -> None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(delay)
                await self.refresh_token()

        with contextlib.suppress(RuntimeError):  # no running loop yet
            self._refresh_task = asyncio.ensure_future(_later())

    async def close(self) -> None:
        """Stop refreshing. Idempotent, and safe on a store that never armed."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._refresh_task
            self._refresh_task = None

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


def http_session_store_builder(**defaults: Any):
    """Registry builder.

    Unlike the mongo one this takes no connection defaults, and that is the
    point: ``base_url`` and ``token`` are per-session values the server mints
    and sends in the descriptor, so an image can register the kind without
    baking in an endpoint or a credential.
    """

    def _build(options: Any = None) -> HttpSessionStore:
        merged = {**defaults, **(dict(options) if isinstance(options, dict) else {})}
        if not merged.get("base_url"):
            raise ValueError(
                "http session store: options.base_url is required — the server sends it per session"
            )
        if not merged.get("token"):
            raise ValueError(
                "http session store: options.token is required — the server mints it per session"
            )
        return HttpSessionStore(**merged)

    return _build


__all__ = [
    "DEFAULT_APPEND_TIMEOUT",
    "DEFAULT_LOAD_TIMEOUT",
    "TOKEN_HEADER",
    "HttpSessionStore",
    "SessionStoreAppendError",
    "expiry_of",
    "http_session_store_builder",
]
