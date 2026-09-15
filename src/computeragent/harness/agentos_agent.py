"""Resolve an agent's run definition from AgentOS by its Mongo ``_id``.

The single entry point for the SDK's one run mode: give it an AgentOS API base,
an agent id, and a Lyzr workspace key, and it returns everything needed to run
that agent locally — source, harness, the effective model, and the model
gateway it is bound to.

    GET {base_url}/v1/agents/{agent_id}      header: x-api-key: <lyzr key>

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
    #: String URL / path, or an inline identity source. ``None`` when the agent
    #: has no resolvable source (the caller must then pass ``source=``).
    source: Any | None
    #: The model string to put on the wire. For a native gateway this is the
    #: backing Lyzr Studio agent id, NOT the catalog name — see `model_of`.
    model: str | None
    #: ``"native"``, or ``None`` when the agent has no effective gateway.
    gateway_type: str | None
    gateway_base_url: str | None
    #: The server's own ``{baseUrl}{LYZR_UPSTREAM_PATH}``. Preferred over
    #: reconstructing the path client-side.
    gateway_completions_url: str | None
    #: Lyzr provider-credential id bound for cloning a private source. Echoed so
    #: a client knows auth is needed; the SDK cannot resolve it to a token
    #: (that needs an operator service token), so it only ever appears in the
    #: error message when a private clone fails.
    credential_id: str | None
    agent_type: str | None

    @property
    def has_native_gateway(self) -> bool:
        return self.gateway_type == "native"


def api_base(base_url: str) -> str:
    """Normalise whatever the caller passed into the ``/v1`` API root.

    Accepts all three spellings people actually use::

        http://host:8788                      → http://host:8788/agentos/api/v1
        http://host:8788/agentos/api          → http://host:8788/agentos/api/v1
        http://host:8788/agentos/api/v1       → unchanged

    so a trailing slash, or a base that already carries the version, never
    produces a doubled path.
    """
    base = base_url.rstrip("/")
    if not base:
        raise AgentResolveError("base_url is empty — pass the AgentOS API base, e.g. http://host:8788/agentos/api")
    if base.endswith("/v1"):
        return base
    if base.endswith("/agentos/api"):
        return f"{base}/v1"
    return f"{base}/agentos/api/v1"


def model_of(doc: dict[str, Any]) -> str | None:
    """The model string AgentOS would have dispatched.

    Mirrors ``modelForDispatch`` (agent-defs.ts): the gateway binding's
    ``externalModelId`` wins, else its ``model``, else the doc's top-level
    ``model``. For a **native** gateway ``externalModelId`` is the backing Lyzr
    Studio agent id (a Mongo ObjectId) — that is what goes upstream as the
    OpenAI ``model``. The catalog name (``"Anthropic:anthropic/claude-…"``) only
    ever configured the Studio agent; sending it to the gateway is a 500.

    ``None`` means "nothing bound" — let the GAP repo's ``agent.yaml`` decide.
    """
    gateway = doc.get("model_gateway") or {}
    binding = gateway.get("binding") or {} if isinstance(gateway, dict) else {}
    for candidate in (binding.get("externalModelId"), binding.get("model"), doc.get("model")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


def _require_supported(doc: dict[str, Any], agent_id: str) -> None:
    """Refuse agent shapes this run mode does not implement, naming the field.

    A *null* gateway is fine — that is a direct-Anthropic run, exactly what the
    TS ``defaultEnvsFor`` produces when nothing resolves. Only a **generic**
    gateway is refused, because its upstream contract differs.
    """
    harness = doc.get("harness")
    if harness not in BUILTIN_HARNESSES:
        raise UnsupportedAgentError(
            f"agent {agent_id}: harness={harness!r} has no Python implementation in this SDK "
            f"(built-ins: {', '.join(sorted(BUILTIN_HARNESSES))}). Pass engine= with a custom "
            "EngineDriver to run it anyway."
        )

    gateway = doc.get("model_gateway")
    if isinstance(gateway, dict):
        gateway_type = gateway.get("type")
        if gateway_type != "native":
            raise UnsupportedAgentError(
                f"agent {agent_id}: model_gateway.type={gateway_type!r} is not supported — this "
                "run mode implements native gateways (and no gateway at all), not generic ones."
            )

    mcp_servers = doc.get("mcp_servers")
    if isinstance(mcp_servers, list) and mcp_servers:
        raise UnsupportedAgentError(
            f"agent {agent_id}: mcp_servers is non-empty ({len(mcp_servers)} bound) and MCP is not "
            "supported in this run mode. The servers would be silently dropped, so the run is "
            "refused instead."
        )

    if doc.get("orchestration") is not None:
        raise UnsupportedAgentError(
            f"agent {agent_id}: orchestration is set and sub-agent fan-out is not supported in this "
            "run mode. The agent would run as a plain single agent, which is not what it declares."
        )


def _require_consistent_gateway(doc: dict[str, Any], agent_id: str) -> None:
    """Port of ``requireConsistentGatewayBinding`` (agent-defs.ts:220).

    A binding that names an ``externalModelId`` while its gateway failed to
    resolve is the one case where the *catalog* name would reach the gateway
    and 500 it. Server-side that is a 503 before dispatch; here it is a refusal
    before boot.
    """
    gateway = doc.get("model_gateway")
    if isinstance(gateway, dict):
        return  # gateway resolved — nothing to reconcile
    if doc.get("modelGatewayId"):
        raise UnsupportedAgentError(
            f"agent {agent_id}: bound to modelGatewayId={doc['modelGatewayId']!r} but the server "
            "returned no model_gateway — the binding did not resolve for this caller. Check that "
            "the agent's ownerGroup matches a group with a configured gateway."
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

    try:
        if client is not None:
            resp = client.get(url, headers=headers, timeout=timeout)
        else:
            resp = httpx.get(url, headers=headers, timeout=timeout)
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
        raise AgentResolveError(f"AgentOS returned {type(doc).__name__}, expected an object, from {url}")

    _require_supported(doc, agent_id)
    _require_consistent_gateway(doc, agent_id)

    gateway = doc.get("model_gateway") if isinstance(doc.get("model_gateway"), dict) else None
    resolved = ResolvedAgent(
        agent_id=str(doc.get("id") or agent_id),
        name=str(doc.get("name") or agent_id),
        harness=str(doc.get("harness") or "claude-agent-sdk"),
        source=doc.get("source") or doc.get("sourceUrl"),
        model=model_of(doc),
        gateway_type=(gateway or {}).get("type"),
        gateway_base_url=(gateway or {}).get("baseUrl"),
        gateway_completions_url=(gateway or {}).get("completionsUrl"),
        credential_id=doc.get("credential_id"),
        agent_type=doc.get("type"),
    )
    logger.debug(
        "resolved agent %s name=%r harness=%s gateway=%s model=%s",
        agent_id, resolved.name, resolved.harness, resolved.gateway_type, resolved.model,
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
    "AgentResolveError",
    "ResolvedAgent",
    "UnsupportedAgentError",
    "api_base",
    "model_of",
    "resolve_agent",
]
