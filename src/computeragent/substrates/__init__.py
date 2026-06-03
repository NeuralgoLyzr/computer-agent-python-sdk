"""Substrate registry — resolves string labels to :class:`Substrate`
instances. Third-party substrates can ``register_substrate("e2b", ...)``
at import time and the harness will accept ``runtime="e2b"``.
"""

from __future__ import annotations

import logging

from ..harness.errors import UnknownSubstrateError
from ..protocol_types import Substrate
from .local import LocalSubstrate

logger = logging.getLogger("computeragent.substrates")

_REGISTRY: dict[str, Substrate] = {}


def register_substrate(name: str, substrate: Substrate) -> None:
    """Register a substrate under ``name``. Replaces any prior entry."""
    if not isinstance(substrate, Substrate):
        raise TypeError(
            f"register_substrate({name!r}): instance does not satisfy "
            "the Substrate Protocol"
        )
    _REGISTRY[name] = substrate
    logger.debug("registered substrate %r", name)


def resolve_substrate(name: str) -> Substrate:
    """Look up a substrate by name, raising :class:`UnknownSubstrateError`
    if no plugin matches."""
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise UnknownSubstrateError(
            f"unknown substrate {name!r}; registered: {sorted(_REGISTRY)!r}"
        ) from exc


# Built-in plugins.
register_substrate("local", LocalSubstrate())


__all__ = ["LocalSubstrate", "register_substrate", "resolve_substrate"]
