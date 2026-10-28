"""Build the engine environment that sends every model call to ``llm_url``.

``llm_url`` is the egress route for model traffic. It is handed to the engine
verbatim: ``ANTHROPIC_BASE_URL`` for an Anthropic-shaped engine, and
``GITCLAW_MODEL_BASE_URL`` / ``GITAGENT_MODEL_BASE_URL`` for gitagent. The
agent document's own gateway URL is not used, and there is no direct path to a
model provider: what sits behind ``llm_url`` is the egress proxy's choice.

The model string still follows the TS dispatch (``modelForDispatch``): a Lyzr
Studio agent id for a native gateway, ``openai:``-prefixed for gitagent.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .agentos_agent import ResolvedAgent

#: Engines that speak the Anthropic Messages shape.
ANTHROPIC_SHAPED = frozenset({"claude-agent-sdk", "claude-code", "deepagents"})

_PROVIDER_PREFIX = re.compile(r"^[a-z][a-z0-9_-]*:", re.IGNORECASE)


class GatewayConfigError(RuntimeError):
    """The model route cannot produce a working run."""


def strip_provider_prefix(model: str) -> str:
    """Drop a leading ``provider:`` segment, mirroring the TS Claude engine.

    ``engine-claude-agent-sdk/src/engine.ts:132`` applies exactly this regex
    before handing the model to the CLI. It is a no-op on a Lyzr Studio agent id
    (a bare ObjectId, no colon) and turns
    ``"Anthropic:anthropic/claude-sonnet-4-6"`` into
    ``"anthropic/claude-sonnet-4-6"``.
    """
    return _PROVIDER_PREFIX.sub("", model, count=1)


def dispatch_model(agent: ResolvedAgent, *, engine_name: str) -> str | None:
    """The model string to hand the engine, per ``modelForDispatch``.

    gitclaw resolves its provider from the model prefix, and every model call
    goes to ``llm_url`` (the agent gateway, OpenAI-shaped for gitagent), so a
    model with no prefix gets ``openai:`` — that is how it finds
    ``OPENAI_API_KEY`` instead of failing with "No API key for provider". A
    prefixed model is unchanged. The Claude engine wants the opposite: any
    prefix stripped.
    """
    model = agent.model
    if model is None:
        return None
    if engine_name == "gitagent":
        if ":" not in model:
            return f"openai:{model}"
        return model
    return strip_provider_prefix(model)


def build_gateway_envs(*, api_key: str, llm_url: str) -> dict[str, str]:
    """Environment variables that send the engine's model calls to ``llm_url``."""
    if not isinstance(llm_url, str) or not llm_url.startswith(("http://", "https://")):
        raise GatewayConfigError(
            f"llm_url must be an http(s) URL, got {llm_url!r}. Every model call is sent to it, "
            "so a malformed value would not fail until the engine tried to reach it mid-run."
        )
    base = llm_url.rstrip("/")
    return {
        "ANTHROPIC_BASE_URL": base,
        "GITAGENT_MODEL_BASE_URL": base,
        "GITCLAW_MODEL_BASE_URL": base,
        # One credential under three names: the engine presents it on every
        # model call and the egress proxy swaps it for the real key.
        "ANTHROPIC_API_KEY": api_key,
        "OPENAI_API_KEY": api_key,
        "LYZR_API_KEY": api_key,
        # The Claude CLI otherwise injects Claude-Code-only experimental betas
        # (a `context_management` body field plus beta headers) that a
        # spec-compliant gateway rejects outright with
        # `400 context_management: Extra inputs are not permitted`.
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        # The Claude CLI's own background traffic (telemetry, error reporting,
        # update checks) goes straight to Anthropic and Datadog, around
        # llm_url. Every call must go through the egress proxy.
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }


def merge_envs(
    caller_envs: Mapping[str, str] | None,
    gateway_envs: Mapping[str, str],
) -> dict[str, str]:
    """Layer gateway envs over the caller's, matching the TS merge order.

    ``llm_url`` takes full ownership of LLM routing, so
    ``CLAUDE_CODE_USE_BEDROCK`` is dropped: the Claude CLI routes through
    Bedrock whenever that flag is set and ignores ``ANTHROPIC_BASE_URL``
    entirely, which would send model calls around the egress proxy.
    """
    merged: dict[str, str] = dict(caller_envs or {})
    merged.update(gateway_envs)
    if gateway_envs.get("ANTHROPIC_BASE_URL"):
        merged.pop("CLAUDE_CODE_USE_BEDROCK", None)
    return merged


def build_options(
    agent: ResolvedAgent,
    *,
    engine_name: str,
    caller_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Engine options for the run, mirroring ``sandboxBodyFor``'s ``options``.

    ``permission_mode``/``setting_sources`` are what AgentOS dispatches; the
    resolved model is a caller-level override and so outranks the GAP repo's own
    ``model.preferred``. An explicit ``options=`` from the caller outranks both.
    """
    options: dict[str, Any] = {
        "permission_mode": "bypassPermissions",
        "setting_sources": ["project"],
    }
    model = dispatch_model(agent, engine_name=engine_name)
    if model:
        options["model"] = model
    options.update(dict(caller_options or {}))
    return options


__all__ = [
    "ANTHROPIC_SHAPED",
    "GatewayConfigError",
    "build_gateway_envs",
    "build_options",
    "dispatch_model",
    "merge_envs",
    "strip_provider_prefix",
]
