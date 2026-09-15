"""Build the engine environment for a gateway-bound agent.

A faithful port of ``modelGatewayEnvsFor`` (agentos-server ``agent-defs.ts:718``)
— the function that decides how a locally-run engine reaches its model. Getting
a byte wrong here costs a live debugging session, so the shapes below are
copied deliberately rather than derived.

There are three branches, and the LLM proxy appears in exactly one:

===========================  ======================================  ===========
case                         base URL                                proxy
===========================  ======================================  ===========
claude-agent-sdk + native    ``{proxy}/up/{b64url(gateway_base)}``    required
gitagent + native            ``{gateway_base}/v4``                    not used
any engine, no gateway       none — direct Anthropic                  not used
===========================  ======================================  ===========

Why the proxy exists at all: ``claude-agent-sdk`` speaks the Anthropic Messages
wire shape and has no OpenAI-shaped fallback, while a Lyzr native gateway is an
OpenAI ``/v4/chat/completions`` endpoint. Pointing the Claude CLI at the gateway
directly 405s. The proxy translates. ``gitagent`` needs none of this: gitclaw
speaks OpenAI natively and redirects via its own ``GITCLAW_MODEL_BASE_URL``.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from typing import Any

from .agentos_agent import ResolvedAgent

#: Engines that speak the Anthropic Messages shape and therefore need the
#: translating proxy in front of an OpenAI-shaped gateway.
ANTHROPIC_SHAPED = frozenset({"claude-agent-sdk", "claude-code", "deepagents"})

#: Path the proxy appends to the decoded ``/up/`` base. Only documented here —
#: the SDK never builds it, the proxy owns it. Present so the doc-comment above
#: is checkable against ``LYZR_UPSTREAM_PATH``.
UPSTREAM_PATH = "/v4/chat/completions"

_PROVIDER_PREFIX = re.compile(r"^[a-z][a-z0-9_-]*:", re.IGNORECASE)


class GatewayConfigError(RuntimeError):
    """The (engine, gateway, proxy) combination cannot produce a working run."""


def b64url(value: str) -> str:
    """Node's ``Buffer.toString("base64url")`` — URL alphabet, **no padding**.

    The proxy decodes this segment with the same alphabet and rejects anything
    that does not parse as an http(s) URL, so the padding difference is not
    cosmetic.
    """
    return base64.urlsafe_b64encode(value.encode("utf-8")).rstrip(b"=").decode("ascii")


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

    gitclaw resolves its provider from the model prefix, so a gateway model with
    no prefix gets ``openai:`` — that is how an OpenAI-shaped gateway model
    finds ``OPENAI_API_KEY`` instead of failing with "No API key for provider".
    The Claude engine wants the opposite: any prefix stripped.
    """
    model = agent.model
    if model is None:
        return None
    if engine_name == "gitagent":
        if agent.has_native_gateway and ":" not in model:
            return f"openai:{model}"
        return model
    return strip_provider_prefix(model)


def build_gateway_envs(
    agent: ResolvedAgent,
    *,
    engine_name: str,
    api_key: str,
    llm_proxy_url: str | None = None,
) -> dict[str, str]:
    """Environment variables the engine subprocess needs to reach the model.

    Returns ``{}`` for an agent with no effective gateway — a direct-Anthropic
    run, where the caller supplies ``ANTHROPIC_API_KEY`` through ``envs=``.
    """
    if not agent.has_native_gateway:
        return {}

    base = (agent.gateway_base_url or "").rstrip("/")
    if not base:
        raise GatewayConfigError(
            f"agent {agent.agent_id}: the model gateway resolved but carries no baseUrl — "
            "there is nothing to route model calls at."
        )

    anthropic_shaped = engine_name in ANTHROPIC_SHAPED

    # gitclaw appends only "/chat/completions" itself, so its base must already
    # carry the "/v4". The proxy, by contrast, is handed the BARE host and adds
    # its own LYZR_UPSTREAM_PATH — encoding "{base}/v4" there yields "/v4/v4".
    gitclaw_base = f"{base}/v4"

    if anthropic_shaped:
        if not llm_proxy_url:
            raise GatewayConfigError(
                f"agent {agent.agent_id} is bound to a native model gateway and engine "
                f"{engine_name!r} speaks the Anthropic Messages shape, so it needs the "
                "Anthropic-to-OpenAI translating proxy: pass llm_proxy_url= (e.g. "
                "http://127.0.0.1:8786). Without it the engine would POST Anthropic Messages "
                f"at {base}, which is an OpenAI endpoint, and get a 405."
            )
        anthropic_base = f"{llm_proxy_url.rstrip('/')}/up/{b64url(base)}"
    else:
        anthropic_base = gitclaw_base

    return {
        "ANTHROPIC_BASE_URL": anthropic_base,
        # Set for every branch, exactly as the TS does. Inert for an
        # Anthropic-shaped engine, which reads only ANTHROPIC_BASE_URL.
        "GITAGENT_MODEL_BASE_URL": gitclaw_base if not anthropic_shaped else base,
        "GITCLAW_MODEL_BASE_URL": gitclaw_base if not anthropic_shaped else base,
        # One credential under three names. ANTHROPIC_API_KEY is overloaded — on
        # a gateway run it holds a Lyzr key, on a direct run a real sk-ant- one —
        # so LYZR_API_KEY carries the same value under a name that means what it
        # is, for anything that needs "the caller's Lyzr credential" specifically.
        "ANTHROPIC_API_KEY": api_key,
        "OPENAI_API_KEY": api_key,
        "LYZR_API_KEY": api_key,
        # The Claude CLI otherwise injects Claude-Code-only experimental betas
        # (a `context_management` body field plus beta headers) that a
        # spec-compliant gateway rejects outright with
        # `400 context_management: Extra inputs are not permitted`.
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }


def merge_envs(
    caller_envs: Mapping[str, str] | None,
    gateway_envs: Mapping[str, str],
) -> dict[str, str]:
    """Layer gateway envs over the caller's, matching the TS merge order.

    A resolved gateway takes full ownership of LLM routing, so
    ``CLAUDE_CODE_USE_BEDROCK`` is dropped: the Claude CLI routes through
    Bedrock whenever that flag is set and ignores ``ANTHROPIC_BASE_URL``
    entirely, which would send a gateway agent's model id to Bedrock and have it
    rejected as an invalid Bedrock model — with nothing in the config looking
    wrong.
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
    "UPSTREAM_PATH",
    "GatewayConfigError",
    "b64url",
    "build_gateway_envs",
    "build_options",
    "dispatch_model",
    "merge_envs",
    "strip_provider_prefix",
]
