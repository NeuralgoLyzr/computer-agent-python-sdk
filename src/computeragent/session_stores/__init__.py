"""Session-store registry — names and wire descriptors → live stores.

A store is registered as a **builder**, not an instance, because the two stores
that matter are configured per call rather than per process: ``mongo`` varies by
database/collection, and ``http`` carries a per-session base URL and capability
token. An instance may still be registered directly (it is wrapped in a builder
that ignores options) for the stateless case, which is how ``memory`` works.

Mirrors the TS ``createHarnessServer({ sessionStores })`` registry and its
per-kind builders.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from ..harness.errors import UnknownSessionStoreError
from ..protocol_types import SessionStore
from .memory import InMemorySessionStore

logger = logging.getLogger("computeragent.session_stores")

#: Builds a store from the wire descriptor's ``options`` (``None`` when a bare
#: name was given).
SessionStoreBuilder = Callable[[Any], SessionStore]

_REGISTRY: dict[str, SessionStoreBuilder] = {}


def is_session_store(candidate: Any) -> bool:
    """Duck-typed check for the upstream ``SessionStore`` contract.

    Deliberately not ``isinstance``: upstream declares the Protocol
    non-runtime-checkable because only ``append`` and ``load`` are required and
    call sites probe for the rest. A store need not subclass anything, so the
    honest test is whether those two are callable.
    """
    return callable(getattr(candidate, "append", None)) and callable(
        getattr(candidate, "load", None)
    )


def register_session_store(name: str, store: SessionStore | SessionStoreBuilder) -> None:
    """Register a store kind, as either a builder or a ready instance."""
    if is_session_store(store):
        instance = store

        def _fixed(_options: Any = None) -> SessionStore:
            return instance

        _REGISTRY[name] = _fixed
    elif callable(store):
        _REGISTRY[name] = store
    else:
        raise TypeError(
            f"register_session_store({name!r}): expected a SessionStore instance or a "
            "builder callable taking the descriptor's options"
        )
    logger.debug("registered session store %r", name)


def resolve_session_store(spec: str | Mapping[str, Any]) -> SessionStore:
    """Resolve ``"mongo"`` or ``{"kind": "mongo", "options": {...}}`` to a store."""
    if isinstance(spec, Mapping):
        kind = spec.get("kind")
        options = spec.get("options")
        if not isinstance(kind, str) or not kind:
            raise UnknownSessionStoreError(
                f"session store descriptor needs a string 'kind'; got {spec!r}"
            )
    else:
        kind, options = spec, None

    try:
        builder = _REGISTRY[kind]
    except KeyError as exc:
        raise UnknownSessionStoreError(
            f"unknown session store {kind!r}; registered: {sorted(_REGISTRY)!r}"
        ) from exc
    return builder(options)


# Built-ins. `memory` is stateless config-wise, so one instance serves every
# caller — which also makes it a shared store across agents in one process,
# the same as the TS in-process default.
register_session_store("memory", InMemorySessionStore())


# `mongo` and `http` are registered as builders that import their driver only
# when actually built, so a local-only install never pays for pymongo or httpx
# at package import — and a caller who asks for one without the extra gets a
# clean ImportError naming it rather than a missing-kind error.
def _mongo_builder(options: Any = None) -> SessionStore:
    from .mongo import mongo_session_store_builder

    return mongo_session_store_builder()(options)


def _http_builder(options: Any = None) -> SessionStore:
    from .http import http_session_store_builder

    return http_session_store_builder()(options)


register_session_store("mongo", _mongo_builder)
register_session_store("http", _http_builder)


__all__ = [
    "InMemorySessionStore",
    "is_session_store",
    "SessionStoreBuilder",
    "register_session_store",
    "resolve_session_store",
]
