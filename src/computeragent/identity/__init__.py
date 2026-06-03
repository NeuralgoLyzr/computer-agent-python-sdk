"""Identity-loader registry — string labels → :class:`IdentityLoader`
instances."""

from __future__ import annotations

import logging

from ..harness.errors import UnknownIdentityLoaderError
from ..protocol_types import IdentityLoader
from .gap import GapIdentityLoader
from .passthrough import PassthroughLoader

logger = logging.getLogger("computeragent.identity")

_REGISTRY: dict[str, IdentityLoader] = {}


def register_identity_loader(name: str, loader: IdentityLoader) -> None:
    if not isinstance(loader, IdentityLoader):
        raise TypeError(
            f"register_identity_loader({name!r}): instance does not satisfy "
            "the IdentityLoader Protocol"
        )
    _REGISTRY[name] = loader
    logger.debug("registered identity loader %r", name)


def resolve_identity_loader(name: str) -> IdentityLoader:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise UnknownIdentityLoaderError(
            f"unknown identity loader {name!r}; registered: {sorted(_REGISTRY)!r}"
        ) from exc


# Built-ins.
register_identity_loader("passthrough", PassthroughLoader())
register_identity_loader("gap", GapIdentityLoader())


__all__ = [
    "GapIdentityLoader",
    "PassthroughLoader",
    "register_identity_loader",
    "resolve_identity_loader",
]
