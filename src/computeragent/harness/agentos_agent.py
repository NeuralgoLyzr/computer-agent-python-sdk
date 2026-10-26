"""Resolve an agent's run definition from AgentOS by its Mongo ``_id``.

The single entry point for the SDK's one run mode: give it an AgentOS API base,
an agent id, and a Lyzr workspace key, and it returns everything needed to run
that agent locally: the harness, the model and the MCP servers. The agent itself
is baked into the image, so the document's source is not read.

Fields read: ``id``, ``name``, ``harness``, ``type``, ``orchestration``, and
the gateway sections ``llm`` (``llm.config.model``) and ``mcp``
(``mcp.servers``). ``guardrails`` is not read: the agent gateway applies it.
A document without these sections resolves with no model and no MCP servers.

    GET {base_url}/v1/agents/{agent_id}      header: x-api-key: <lyzr key>

With the egress base ``http://egress/computer-agent-api/agentos/api`` that is
``GET http://egress/computer-agent-api/agentos/api/v1/agents/{id}``, which the
egress proxy forwards to the computer-agent-api-service.

The response is the authority; the SDK reproduces what the AgentOS dispatch
path (``sandboxBodyFor`` / ``modelGatewayEnvsFor`` in the TS ``agent-defs.ts``)
would have sent to a ComputerAgent server, but runs the engine in-process
instead.

**Credential.** The key travels on ``x-api-key``. AgentOS reads
``Authorization: Bearer`` first and falls back to ``x-api-key``, but a bare
Lyzr workspace key is not a ``cak_`` key — it resolves through the PAGOS bridge,
which is the path ``x-api-key`` is the canonical header for.

**Fail fast, and name the field.** Resolution happens in
``ComputerAgent.__init__`` before anything is booted, so every refusal here is
cheap and attributable. An agent shape this run mode does not implement is
refused by name rather than run as something subtly different from what AgentOS
would have dispatched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .mcp import McpBinding, McpBindingError, parse_bindings

logger = logging.getLogger("computeragent.agentos_agent")

# Engines this SDK can actually run in-process. An agent bound to anything else
# (`deepagents`, a custom TS engine) resolves fine server-side but has no Python
# implementation here.
BUILTIN_HARNESSES = frozenset({"claude-agent-sdk", "gitagent"})


class AgentResolveError(RuntimeError):
    """Resolution could not proceed: bad config, transport failure, or an HTTP
    error from AgentOS. Carries a message safe to show a user — never the key."""


class UnsupportedAgentError(AgentResolveError):
    """The agent resolved, but its shape is one this run mode does not
    implement. The message always names the offending field."""


@dataclass(frozen=True)
class ResolvedAgent:
    """What ``GET /v1/agents/{id}`` tells us about how to run an agent.

    Deliberately holds no credential — the caller's key stays in
    :class:`~computeragent.harness.ComputerAgent`, so a ``repr()`` of this
    object can never leak it into a log or a traceback.
    """

    agent_id: str
    name: str
    harness: str
    #: The model string to put on the wire: the gateway agent's kong model
    #: (``llm.config.model``), or ``None`` to use the image's own model.
    model: str | None
    agent_type: str | None
    #: MCP servers bound to the agent (``mcp.servers``), as AgentOS resolved
    #: them for the caller.
    mcp_bindings: tuple[McpBinding, ...] = ()


def api_base(base_url: str) -> str:
    """Normalise whatever the caller passed into the ``/v1`` API root.

    Accepts all three spellings people actually use::

        http://host:8788                      → http://host:8788/agentos/api/v1
        http://host:8788/agentos/api          → http://host:8788/agentos/api/v1
        http://host:8788/agentos/api/v1       → unchanged

    so a trailing slash, or a base that already carries the version, never
    produces a doubled path.
    """
    if not isinstance(base_url, str):
        raise AgentResolveError(
            f"base_url must be a string, got {type(base_url).__name__} — pass the AgentOS API "
            "base, e.g. http://host:8788/agentos/api"
        )
    base = base_url.rstrip("/")
    if not base:
        raise AgentResolveError(
            "base_url is empty — pass the AgentOS API base, e.g. http://host:8788/agentos/api"
        )
    if base.endswith("/v1"):
        return base
    if base.endswith("/agentos/api"):
        return f"{base}/v1"
    # Only a BARE ORIGIN gets the standard path appended. Appending to anything
    # else turned one typo — `…/agentos/apiv1` — into
    # `…/agentos/apiv1/agentos/api/v1`, and the 404 that came back blamed the
    # caller's agent id, sending them to look in entirely the wrong place.
    parsed = urlsplit(base)
    if parsed.path:
        raise AgentResolveError(
            f"base_url {base_url!r} has a path this SDK does not recognise. Expected a bare origin "
            "(http://host:8788), the API base (…/agentos/api), or the versioned base "
            "(…/agentos/api/v1)."
        )
    return f"{base}/agentos/api/v1"


def model_of(doc: dict[str, Any]) -> str | None:
    """The model the agent gateway accepts for this agent: ``llm.config.model``.

    ``None`` when ``llm`` or its ``config`` is absent, null or not an object, or
    the model is blank: the image's own model is used (the GAP ``agent.yaml``
    ``model.preferred``). ``llm.provider`` is not read.
    """
    llm = doc.get("llm")
    config = llm.get("config") if isinstance(llm, dict) else None
    model = config.get("model") if isinstance(config, dict) else None
    return model if isinstance(model, str) and model.strip() else None


def require_runnable_harness(agent: ResolvedAgent, *, engine_given: bool) -> None:
    """Refuse a harness with no Python implementation — unless the caller
    brought their own engine.

    Deliberately NOT part of document validation: whether a harness can run is a
    property of (document, caller's engine), not of the document alone. Checking
    it during resolution made the documented escape hatch a lie — the error told
    you to pass ``engine=``, and passing it changed nothing because resolution
    had already refused.
    """
    if engine_given or agent.harness in BUILTIN_HARNESSES:
        return
    raise UnsupportedAgentError(
        f"agent {agent.agent_id}: harness={agent.harness!r} has no Python implementation in this "
        f"SDK (built-ins: {', '.join(sorted(BUILTIN_HARNESSES))}). Pass engine= with your own "
        "EngineDriver to run it anyway, or re-register the agent on a supported harness."
    )


def _require_supported(doc: dict[str, Any], agent_id: str) -> None:
    """Refuse agent shapes this run mode does not implement, naming the field."""
    if doc.get("orchestration") is not None:
        raise UnsupportedAgentError(
            f"agent {agent_id}: orchestration is set and sub-agent fan-out is not supported in this "
            "run mode. The agent would run as a plain single agent, which is not what it declares."
        )


def resolve_agent(
    agent_id: str,
    *,
    base_url: str,
    api_key: str,
    timeout: float = 15.0,
    client: Any | None = None,
) -> ResolvedAgent:
    """Fetch and validate an agent's run definition. Raises, never returns None.

    ``client`` is an injection point for tests; production passes nothing and a
    one-shot ``httpx`` request is made.
    """
    if not agent_id or not agent_id.strip():
        raise AgentResolveError("agent_id is required")
    if not api_key or not api_key.strip():
        raise AgentResolveError(
            "api_key is required — pass your Lyzr workspace key (it travels as x-api-key)"
        )

    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - httpx ships with the [agentos] extra
        raise AgentResolveError(
            "resolving an agent requires httpx — install 'computer-agent-py[agentos]'"
        ) from exc

    url = f"{api_base(base_url)}/agents/{agent_id}"
    headers = {"x-api-key": api_key, "accept": "application/json"}

    from .egress_token import egress_event_hooks, install

    try:
        if client is not None:
            resp = install(client).get(url, headers=headers, timeout=timeout)
        else:
            with httpx.Client(event_hooks=egress_event_hooks(False)) as owned:
                resp = owned.get(url, headers=headers, timeout=timeout)
    except Exception as exc:  # network / DNS / TLS — never leak the key in the message
        raise AgentResolveError(
            f"could not reach AgentOS at {url} ({type(exc).__name__}) — check base_url and that "
            "the server is running"
        ) from None

    _raise_for_status(resp, agent_id=agent_id, url=url)

    try:
        doc: dict[str, Any] = resp.json()
    except Exception as exc:
        raise AgentResolveError(f"AgentOS returned a non-JSON body from {url}") from exc
    if not isinstance(doc, dict):
        raise AgentResolveError(
            f"AgentOS returned {type(doc).__name__}, expected an object, from {url}"
        )

    _require_supported(doc, agent_id)

    try:
        mcp_bindings = parse_bindings(doc.get("mcp"))
    except McpBindingError as exc:
        raise UnsupportedAgentError(f"agent {agent_id}: {exc}") from None

    resolved = ResolvedAgent(
        agent_id=str(doc.get("id") or agent_id),
        name=str(doc.get("name") or agent_id),
        harness=str(doc.get("harness") or "claude-agent-sdk"),
        model=model_of(doc),
        agent_type=doc.get("type"),
        mcp_bindings=mcp_bindings,
    )
    logger.debug(
        "resolved agent %s name=%r harness=%s model=%s",
        agent_id,
        resolved.name,
        resolved.harness,
        resolved.model,
    )
    return resolved


def _raise_for_status(resp: Any, *, agent_id: str, url: str) -> None:
    """Turn AgentOS's error envelope into a message that says what to fix.

    Each status has exactly one likely cause here, and saying which saves the
    caller a round of guessing — a 401 on a valid-looking key almost always
    means the deployment has no PAGOS bridge configured, and a 404 on a real
    agent almost always means a stable ``agentId`` was passed where the route
    wants the Mongo ``_id``.
    """
    status = getattr(resp, "status_code", 0)
    if status < 400:
        return

    code = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            code = str((body.get("error") or {}).get("code") or "")
    except Exception:  # noqa: BLE001 - a non-JSON error body is still an error
        pass

    if status == 401:
        raise AgentResolveError(
            f"AgentOS rejected the credential (401 {code or 'UNAUTHENTICATED'}) for {url}. A bare "
            "Lyzr workspace key authenticates only where the PAGOS bridge is configured on the "
            "server (PAGOS_REDIS_URL / PAGOS_RESOLVE_URL); without it every workspace key 401s."
        )
    if status == 403 and code == "NOT_OWNER":
        raise AgentResolveError(
            f"agent {agent_id} exists but is owned by another group (403 NOT_OWNER). The key's "
            "groups must include the agent's ownerGroup."
        )
    if status == 403:
        raise AgentResolveError(
            f"the key lacks permission to read agent {agent_id} (403 "
            f"{code or 'INSUFFICIENT_PERMISSION'}) — it needs `agents:read`."
        )
    if status == 404:
        raise AgentResolveError(
            f"no agent {agent_id} (404). Note the route wants the 24-hex Mongo `_id`, not the "
            "stable `agentId` — a non-ObjectId is rejected outright."
        )
    raise AgentResolveError(f"AgentOS returned HTTP {status} {code} from {url}")


__all__ = [
    "BUILTIN_HARNESSES",
    "require_runnable_harness",
    "AgentResolveError",
    "ResolvedAgent",
    "UnsupportedAgentError",
    "api_base",
    "model_of",
    "resolve_agent",
]
