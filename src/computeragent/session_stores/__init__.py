"""Session-store registry — string labels → :class:`SessionStore` instances."""

from __future__ import annotations

import logging

from ..harness.errors import UnknownSessionStoreError
from ..protocol_types import SessionStore
from .memory import InMemorySessionStore

logger = logging.getLogger("computeragent.session_stores")

_REGISTRY: dict[str, SessionStore] = {}


def register_session_store(name: str, store: SessionStore) -> None:
    if not isinstance(store, SessionStore):
        raise TypeError(
            f"register_session_store({name!r}): instance does not satisfy "
            "the SessionStore Protocol"
        )
    _REGISTRY[name] = store
    logger.debug("registered session store %r", name)


def resolve_session_store(name: str) -> SessionStore:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise UnknownSessionStoreError(
            f"unknown session store {name!r}; registered: {sorted(_REGISTRY)!r}"
        ) from exc


# Built-ins.
register_session_store("memory", InMemorySessionStore())


__all__ = [
    "InMemorySessionStore",
    "register_session_store",
    "resolve_session_store",
]
