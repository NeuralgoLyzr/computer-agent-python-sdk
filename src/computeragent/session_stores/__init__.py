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


def register_session_store(name: str, store: SessionStore | SessionStoreBuilder) -> None:
    """Register a store kind, as either a builder or a ready instance."""
    if isinstance(store, SessionStore):
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


__all__ = [
    "InMemorySessionStore",
    "SessionStoreBuilder",
    "register_session_store",
    "resolve_session_store",
]
