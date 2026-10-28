"""Sandbox tokens reissued by the egress proxy, adopted by every client.

An answer from the egress proxy may carry a successor to the token the
request was sent with (``X-Cas-Token``, ``X-Cas-Token-Expires-At``). Once one
is adopted, every later egress request from every SDK client carries the
newest token of that chain.

:data:`CREDENTIAL` keeps **chains**: a root key (the key a caller, the
environment or the sandbox's creator supplied) followed by its reissued
successors, newest last. A successor is adopted only when:

* both headers are present;
* the token starts with ``sbt_`` and is at most 4096 bytes;
* the expiry is RFC 3339 with a zone and in the future;
* the request was sent with the newest token of its chain, or with a key in
  no chain yet (which becomes the root of a new chain);
* the expiry is later than the chain's newest.

A chain whose newest expiry has passed is dropped. When ``LYZR_API_KEY``
holds a token of the chain it is set to the successor, so processes spawned
later inherit it. Tokens and header values are never logged.

Clients pick it up through :func:`egress_event_hooks` (owned httpx clients),
:func:`install` (injected httpx clients) or :func:`stamping_session`
(``requests``). Only origins added with :meth:`EgressCredential.configure`
are stamped or observed.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterable, MutableMapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("computeragent.harness.egress_token")

HEADER_TOKEN = "x-cas-token"
HEADER_EXPIRES_AT = "x-cas-token-expires-at"
TOKEN_PREFIX = "sbt_"
MAX_TOKEN_BYTES = 4096

_ENV_KEY = "LYZR_API_KEY"
_BEARER = "bearer "


def origin_of(url: str) -> str | None:
    """``scheme://host[:port]`` of an http(s) URL, lower-cased; ``None`` otherwise."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    default = 443 if parts.scheme == "https" else 80
    suffix = f":{port}" if port and port != default else ""
    return f"{parts.scheme}://{parts.hostname.lower()}{suffix}"


def _parse_expiry(value: str) -> datetime | None:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass
class _Chain:
    tokens: list[str]
    #: The newest token's expiry; ``None`` while only the root is known.
    expires_at: datetime | None = None


@dataclass
class EgressCredential:
    """The process-wide record of reissued sandbox tokens."""

    env: MutableMapping[str, str] = field(default_factory=lambda: os.environ)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _origins: set[str] = field(default_factory=set, repr=False)
    _chains: dict[str, _Chain] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        return f"EgressCredential(origins={sorted(self._origins)!r}, chains={len(set(map(id, self._chains.values())))})"

    # ── origins ──────────────────────────────────────────────────────────

    def configure(self, urls: Iterable[str | None]) -> None:
        """Add the origins of these egress URLs (a union over calls)."""
        with self._lock:
            for url in urls:
                found = origin_of(url) if url else None
                if found:
                    self._origins.add(found)

    def is_egress(self, url: str) -> bool:
        found = origin_of(url)
        with self._lock:
            return found is not None and found in self._origins

    # ── the chains ───────────────────────────────────────────────────────

    def _prune(self, now: datetime) -> None:
        expired = {
            id(chain)
            for chain in self._chains.values()
            if chain.expires_at is not None and chain.expires_at <= now
        }
        if expired:
            self._chains = {k: c for k, c in self._chains.items() if id(c) not in expired}

    def resolve(self, key: str) -> str:
        """The newest token of the chain holding ``key``, else ``key``."""
        with self._lock:
            self._prune(datetime.now(timezone.utc))
            chain = self._chains.get(key)
            return chain.tokens[-1] if chain is not None else key

    def adopt(self, sent: str | None, token: str | None, expires_at: str | None) -> bool:
        """Adopt ``token`` as the successor of ``sent`` when the rules allow."""
        if not sent or not token or not expires_at:
            return self._reject("missing")
        if not token.startswith(TOKEN_PREFIX) or len(token.encode()) > MAX_TOKEN_BYTES:
            return self._reject("bad_token")
        expiry = _parse_expiry(expires_at)
        now = datetime.now(timezone.utc)
        if expiry is None:
            return self._reject("bad_expiry")
        if expiry <= now:
            return self._reject("expired")
        with self._lock:
            self._prune(now)
            chain = self._chains.get(sent)
            if chain is not None and chain.tokens[-1] != sent:
                return self._reject("superseded")
            if token in self._chains:
                return self._reject("known")
            if chain is not None and chain.expires_at is not None and expiry <= chain.expires_at:
                return self._reject("not_later")
            if chain is None:
                chain = _Chain(tokens=[sent])
                self._chains[sent] = chain
            chain.tokens.append(token)
            chain.expires_at = expiry
            self._chains[token] = chain
            if self.env.get(_ENV_KEY) in chain.tokens:
                self.env[_ENV_KEY] = token
        logger.info("sandbox token reissued (expires %s)", expiry.isoformat())
        return True

    @staticmethod
    def _reject(reason: str) -> bool:
        logger.debug("sandbox token reissue not adopted (%s)", reason)
        return False

    # ── requests and answers ─────────────────────────────────────────────

    def stamp(self, url: str, headers: MutableMapping[str, str]) -> None:
        """Replace an egress request's ``x-api-key`` / ``Authorization: Bearer``
        value with the newest token of its chain. Adds no header."""
        if not self.is_egress(url):
            return
        key = headers.get("x-api-key")
        if key:
            newest = self.resolve(key)
            if newest != key:
                headers["x-api-key"] = newest
        auth = headers.get("authorization")
        if auth and auth[: len(_BEARER)].lower() == _BEARER:
            value = auth[len(_BEARER) :]
            newest = self.resolve(value)
            if newest != value:
                headers["authorization"] = f"Bearer {newest}"

    def observe(self, url: str, sent: str | None, headers: Any) -> bool:
        """Adopt a reissue carried by an egress answer."""
        if not self.is_egress(url):
            return False
        token = headers.get(HEADER_TOKEN)
        expires_at = headers.get(HEADER_EXPIRES_AT)
        if token is None and expires_at is None:
            return False
        return self.adopt(sent, token, expires_at)

    def reset(self) -> None:
        """Forget every origin and chain (tests)."""
        with self._lock:
            self._origins.clear()
            self._chains.clear()


#: The one credential record of this process.
CREDENTIAL = EgressCredential()


def sent_key(headers: Any) -> str | None:
    """The credential a request carried: ``x-api-key``, else a Bearer value."""
    key = headers.get("x-api-key")
    if key:
        return str(key)
    auth = headers.get("authorization")
    if auth and str(auth)[: len(_BEARER)].lower() == _BEARER:
        return str(auth)[len(_BEARER) :]
    return None


# ── httpx ────────────────────────────────────────────────────────────────────


def _mark(fn: Any) -> Any:
    fn._computeragent_egress = True
    return fn


def egress_event_hooks(async_: bool) -> dict[str, list[Any]]:
    """``event_hooks`` for an httpx client: stamp each request, observe each answer."""
    if async_:

        async def on_request_async(request: Any) -> None:
            CREDENTIAL.stamp(str(request.url), request.headers)

        async def on_response_async(response: Any) -> None:
            request = response.request
            CREDENTIAL.observe(str(request.url), sent_key(request.headers), response.headers)

        return {"request": [_mark(on_request_async)], "response": [_mark(on_response_async)]}

    def on_request(request: Any) -> None:
        CREDENTIAL.stamp(str(request.url), request.headers)

    def on_response(response: Any) -> None:
        request = response.request
        CREDENTIAL.observe(str(request.url), sent_key(request.headers), response.headers)

    return {"request": [_mark(on_request)], "response": [_mark(on_response)]}


def install(client: Any) -> Any:
    """Add the egress hooks to an existing httpx client; idempotent."""
    hooks = getattr(client, "event_hooks", None)
    if not isinstance(hooks, dict):
        return client
    if any(getattr(fn, "_computeragent_egress", False) for fn in hooks.get("request", [])):
        return client
    try:
        import httpx

        async_ = isinstance(client, httpx.AsyncClient)
    except ImportError:  # pragma: no cover - httpx ships with the [agentos] extra
        return client
    ours = egress_event_hooks(async_)
    client.event_hooks = {
        "request": [*hooks.get("request", []), *ours["request"]],
        "response": [*hooks.get("response", []), *ours["response"]],
    }
    return client


# ── requests ─────────────────────────────────────────────────────────────────


def stamping_session(api_key: Callable[[], str] | None = None) -> Any:
    """A ``requests.Session`` that stamps egress requests and observes their
    answers (``requests`` has response hooks but no request hook). With
    ``api_key``, every egress request first gets ``x-api-key: api_key()``."""
    import requests

    class StampingSession(requests.Session):
        def send(self, request: Any, **kwargs: Any) -> Any:
            url = str(request.url)
            if api_key is not None and CREDENTIAL.is_egress(url):
                request.headers["x-api-key"] = api_key()
            CREDENTIAL.stamp(url, request.headers)
            response = super().send(request, **kwargs)
            CREDENTIAL.observe(url, sent_key(request.headers), response.headers)
            return response

    return StampingSession()


__all__ = [
    "CREDENTIAL",
    "HEADER_EXPIRES_AT",
    "HEADER_TOKEN",
    "EgressCredential",
    "egress_event_hooks",
    "install",
    "origin_of",
    "sent_key",
    "stamping_session",
]
