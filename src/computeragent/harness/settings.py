"""Resolve every :class:`~computeragent.harness.ComputerAgent` setting from its
argument or its environment variable.

The argument always wins; an unset or blank argument falls back to the
variable; a blank variable counts as unset. Every required setting that
resolves to nothing is reported in ONE error naming both spellings, so a
misconfigured image is fixed in one pass rather than one missing value at a
time.

``api_key`` is the exception to "resolve once": when it comes from the
environment it is read again on every :meth:`Settings.api_key` call, so the
value injected into a sandbox can be replaced without rebuilding the agent.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..protocol_types import EngineDriver
from .agentos_agent import AgentResolveError

#: Argument name → environment variable.
ENV_VARS: dict[str, str] = {
    "agent_id": "COMPUTERAGENT_AGENT_ID",
    "engine": "COMPUTERAGENT_ENGINE",
    "base_url": "COMPUTERAGENT_BASE_URL",
    "api_key": "LYZR_API_KEY",
    "gap_dir": "COMPUTERAGENT_GAP_DIR",
}


class SettingsError(AgentResolveError):
    """One or more required settings resolved to nothing."""


def _given(value: object) -> bool:
    if value is None:
        return False
    return not (isinstance(value, str) and not value.strip())


def _env(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(ENV_VARS[name])
    return value if value and value.strip() else None


def _spelling(name: str) -> str:
    return f"{name} (or {ENV_VARS[name]})"


@dataclass(frozen=True, repr=False)
class Settings:
    """The resolved settings. Holds the explicit ``api_key`` only; a key from
    the environment is never stored, it is read when asked for."""

    agent_id: str
    engine: str | EngineDriver
    base_url: str
    gap_dir: str | Path | None
    _explicit_api_key: str | None
    _env: Mapping[str, str]

    def api_key(self) -> str:
        """The key to present, read now: the argument when one was given,
        else the environment variable as it stands at this call."""
        if self._explicit_api_key is not None:
            return self._explicit_api_key
        value = _env(self._env, "api_key")
        if value is None:
            raise SettingsError(f"missing required setting: {_spelling('api_key')}")
        return value

    def __repr__(self) -> str:
        return (
            f"Settings(agent_id={self.agent_id!r}, engine={self.engine!r}, "
            f"base_url={self.base_url!r}, gap_dir={self.gap_dir!r}, api_key=<hidden>)"
        )


def resolve_settings(
    *,
    agent_id: str | None,
    engine: str | EngineDriver | None,
    base_url: str | None,
    api_key: str | None,
    gap_dir: str | Path | None,
    env: Mapping[str, str] | None = None,
) -> Settings:
    """Resolve each setting from its argument, else its environment variable.

    Raises :class:`SettingsError` listing every required setting that is
    missing.
    """
    source = os.environ if env is None else env

    resolved_agent_id = agent_id if _given(agent_id) else _env(source, "agent_id")
    resolved_engine: str | EngineDriver | None = (
        engine if _given(engine) else _env(source, "engine")
    )
    resolved_base_url = base_url if _given(base_url) else _env(source, "base_url")
    explicit_key = api_key if _given(api_key) else None
    resolved_gap_dir: str | Path | None = (
        gap_dir if gap_dir is not None else _env(source, "gap_dir")
    )

    missing = [
        _spelling(name)
        for name, value in (
            ("agent_id", resolved_agent_id),
            ("engine", resolved_engine),
            ("base_url", resolved_base_url),
            ("api_key", explicit_key or _env(source, "api_key")),
        )
        if value is None
    ]
    if missing:
        raise SettingsError("missing required settings: " + ", ".join(missing))

    assert resolved_agent_id is not None
    assert resolved_engine is not None
    assert resolved_base_url is not None
    return Settings(
        agent_id=resolved_agent_id,
        engine=resolved_engine,
        base_url=resolved_base_url,
        gap_dir=resolved_gap_dir,
        _explicit_api_key=explicit_key,
        _env=source,
    )


__all__ = ["ENV_VARS", "Settings", "SettingsError", "resolve_settings"]
