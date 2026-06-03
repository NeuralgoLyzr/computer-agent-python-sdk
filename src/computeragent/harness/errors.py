"""Typed errors raised by the harness coordinator and registries."""

from __future__ import annotations


class HarnessError(Exception):
    """Base class for every harness-side error."""


class UnknownEngineError(HarnessError):
    """``engine="..."`` did not match any registered engine."""


class UnknownSubstrateError(HarnessError):
    """``runtime="..."`` did not match any registered substrate."""


class UnknownSessionStoreError(HarnessError):
    """``session_store="..."`` (string form) did not match any registered store."""


class UnknownIdentityLoaderError(HarnessError):
    """``identity_loader="..."`` did not match any registered loader."""


class HarnessProtocolError(HarnessError):
    """The harness coordinator received an event shape it cannot handle."""


__all__ = [
    "HarnessError",
    "HarnessProtocolError",
    "UnknownEngineError",
    "UnknownIdentityLoaderError",
    "UnknownSessionStoreError",
    "UnknownSubstrateError",
]
