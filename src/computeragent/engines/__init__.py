"""Engine registry — string labels → :class:`EngineDriver` instances.

Built-ins shipped in 0.2.0:

* ``"claude-agent-sdk"`` — wraps ``claude_agent_sdk.query`` directly.
* ``"gitagent"`` — Python re-implementation of the gitclaw loop over an
  OpenAI-compatible endpoint. Requires the ``[gitagent]`` extra (openai).
  The class is exposed eagerly but the OpenAI client is constructed only
  on first ``start_session`` — registering the engine never fails on
  install-time missing extras.
"""

from __future__ import annotations

import logging

from ..harness.errors import UnknownEngineError
from ..protocol_types import EngineDriver
from .claude_agent import ClaudeAgentEngine
from .git_agent import GitAgentEngine

logger = logging.getLogger("computeragent.engines")

_REGISTRY: dict[str, EngineDriver] = {}


def register_engine(name: str, engine: EngineDriver) -> None:
    if not isinstance(engine, EngineDriver):
        raise TypeError(
            f"register_engine({name!r}): instance does not satisfy the EngineDriver Protocol"
        )
    _REGISTRY[name] = engine
    logger.debug("registered engine %r", name)


def resolve_engine(name: str) -> EngineDriver:
    if not isinstance(name, str):
        raise UnknownEngineError(
            f"engine must be a registered name or an EngineDriver instance, got "
            f"{type(name).__name__}. An EngineDriver needs a `name`, `capabilities` and "
            f"`start_session`; registered names: {sorted(_REGISTRY)!r}"
        )
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise UnknownEngineError(
            f"unknown engine {name!r}; registered: {sorted(_REGISTRY)!r}"
        ) from exc


def registered_engines() -> dict[str, EngineDriver]:
    """A copy of the registry: every engine name and its driver."""
    return dict(_REGISTRY)


# Built-ins.
register_engine("claude-agent-sdk", ClaudeAgentEngine())
register_engine("gitagent", GitAgentEngine())


__all__ = [
    "ClaudeAgentEngine",
    "GitAgentEngine",
    "register_engine",
    "registered_engines",
    "resolve_engine",
]
