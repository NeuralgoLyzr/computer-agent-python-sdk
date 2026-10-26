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
    "llm_url": "COMPUTERAGENT_LLM_URL",
    "mcp_url": "COMPUTERAGENT_MCP_URL",
    "guardrails_url": "COMPUTERAGENT_GUARDRAILS_URL",
    "telemetry_url": "COMPUTERAGENT_TELEMETRY_URL",
    "gap_dir": "COMPUTERAGENT_GAP_DIR",
}

#: Egress routes: required, and each must be an http(s) URL.
URL_SETTINGS: tuple[str, ...] = ("llm_url", "mcp_url", "guardrails_url")

#: Egress routes that may be unset (the feature is then off), but must be
#: http(s) URLs when set.
OPTIONAL_URL_SETTINGS: tuple[str, ...] = ("telemetry_url",)


class SettingsError(AgentResolveError):
    """One or more required settings resolved to nothing, or a URL setting is
    not an http(s) URL."""


def _given(value: object) -> bool:
    if value is None:
        return False
    return not (isinstance(value, str) and not value.strip())


def _env(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(ENV_VARS[name])
    return value if value and value.strip() else None


def _optional_url(value: object) -> str | None:
    return str(value).rstrip("/") if value is not None else None


def _spelling(name: str) -> str:
    return f"{name} (or {ENV_VARS[name]})"


@dataclass(frozen=True, repr=False)
class Settings:
    """The resolved settings. Holds the explicit ``api_key`` only; a key from
    the environment is never stored, it is read when asked for."""

    agent_id: str
    engine: str | EngineDriver
    base_url: str
    llm_url: str
    mcp_url: str
    guardrails_url: str
    telemetry_url: str | None
    gap_dir: str | Path | None
    _explicit_api_key: str | None
    _env: Mapping[str, str]

    def api_key(self) -> str:
        """The key to present, read now: the argument when one was given,
        else the environment variable as it stands at this call; either way
        the newest sandbox token of its chain (see ``egress_token``)."""
        from .egress_token import CREDENTIAL

        if self._explicit_api_key is not None:
            return CREDENTIAL.resolve(self._explicit_api_key)
        value = _env(self._env, "api_key")
        if value is None:
            raise SettingsError(f"missing required setting: {_spelling('api_key')}")
        return CREDENTIAL.resolve(value)

    def __repr__(self) -> str:
        return (
            f"Settings(agent_id={self.agent_id!r}, engine={self.engine!r}, "
            f"base_url={self.base_url!r}, llm_url={self.llm_url!r}, mcp_url={self.mcp_url!r}, "
            f"guardrails_url={self.guardrails_url!r}, "
            f"telemetry_url={self.telemetry_url!r}, "
            f"gap_dir={self.gap_dir!r}, api_key=<hidden>)"
        )


def resolve_settings(
    *,
    agent_id: str | None,
    engine: str | EngineDriver | None,
    base_url: str | None,
    api_key: str | None,
    llm_url: str | None,
    mcp_url: str | None,
    guardrails_url: str | None,
    telemetry_url: str | None,
    gap_dir: str | Path | None,
    env: Mapping[str, str] | None = None,
) -> Settings:
    """Resolve each setting from its argument, else its environment variable.

    Raises :class:`SettingsError` listing every required setting that is
    missing, then every URL setting that is not an http(s) URL.
    """
    source = os.environ if env is None else env

    resolved_agent_id = agent_id if _given(agent_id) else _env(source, "agent_id")
    resolved_engine: str | EngineDriver | None = (
        engine if _given(engine) else _env(source, "engine")
    )
    resolved_base_url = base_url if _given(base_url) else _env(source, "base_url")
    given_urls = {
        "llm_url": llm_url,
        "mcp_url": mcp_url,
        "guardrails_url": guardrails_url,
        "telemetry_url": telemetry_url,
    }
    urls = {
        name: given_urls[name] if _given(given_urls[name]) else _env(source, name)
        for name in (*URL_SETTINGS, *OPTIONAL_URL_SETTINGS)
    }
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
            *((name, urls[name]) for name in URL_SETTINGS),
        )
        if value is None
    ]
    if missing:
        raise SettingsError("missing required settings: " + ", ".join(missing))
    malformed = [
        f"{_spelling(name)}={value!r}"
        for name, value in urls.items()
        if value is not None and not str(value).startswith(("http://", "https://"))
    ]
    if malformed:
        raise SettingsError("settings must be http(s) URLs: " + ", ".join(malformed))

    assert resolved_agent_id is not None
    assert resolved_engine is not None
    assert resolved_base_url is not None
    return Settings(
        agent_id=resolved_agent_id,
        engine=resolved_engine,
        base_url=resolved_base_url,
        llm_url=str(urls["llm_url"]).rstrip("/"),
        mcp_url=str(urls["mcp_url"]).rstrip("/"),
        guardrails_url=str(urls["guardrails_url"]).rstrip("/"),
        telemetry_url=_optional_url(urls["telemetry_url"]),
        gap_dir=resolved_gap_dir,
        _explicit_api_key=explicit_key,
        _env=source,
    )


__all__ = [
    "ENV_VARS",
    "OPTIONAL_URL_SETTINGS",
    "URL_SETTINGS",
    "Settings",
    "SettingsError",
    "resolve_settings",
]
