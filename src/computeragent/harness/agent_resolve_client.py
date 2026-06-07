"""Resolve a registered agent's run definition from the AgentOS server by its
stable ``agent_id``.

Lets a caller run an agent *by reference* — ``ComputerAgent(agent_id="…")`` with
no ``source`` — and have the SDK fetch the agent's ``source``/``harness``/``model``
from AgentOS instead of repeating them. The SDK presents the same ``cak_`` API
key it uses everywhere as a bearer token to
``POST {AGENTOS_API_URL}/agents/resolve``.

This is a **synchronous** call by design: resolution happens in
``ComputerAgent.__init__`` (before the coordinator is built), so it must complete
before the constructor returns. Fine for the common script/worker case; a brief
event-loop stall if a ComputerAgent is constructed inside a hot async server.

Unlike the best-effort git-credential resolve, this is **fail-fast**: a miss
(404) returns ``None`` and the caller raises a clear "not registered" error
(agent-id-only is strictly a "run an existing agent" flow).
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("computeragent.agent_resolve")


def _api_base(explicit: str | None) -> str | None:
    base = explicit or os.environ.get("AGENTOS_API_URL")
    return base.rstrip("/") if base else None


def _token(explicit: str | None) -> str | None:
    return (
        explicit
        or os.environ.get("COMPUTERAGENT_HARNESS_TOKEN")
        or os.environ.get("AGENTOS_INGEST_TOKEN")
    )


class AgentResolveError(RuntimeError):
    """Raised when agent resolution cannot proceed (missing config) — distinct
    from a clean miss (which returns ``None``)."""


def resolve_agent(
    agent_id: str,
    *,
    api_base: str | None = None,
    token: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    """Fetch ``{name, source, harness, model?}`` for ``agent_id`` from AgentOS.

    Returns the dict on a hit, ``None`` on a clean miss (404 — agent not
    registered / not readable by the key's group). Raises ``AgentResolveError``
    when the call can't even be made (no ``AGENTOS_API_URL`` or no key), so the
    caller never silently falls through with an unresolved agent.
    """
    base = _api_base(api_base)
    bearer = _token(token)
    if not base:
        raise AgentResolveError(
            "agent_id resolution needs AGENTOS_API_URL (e.g. https://host/agentos/api/v1)"
        )
    if not bearer:
        raise AgentResolveError(
            "agent_id resolution needs an AgentOS API key "
            "(harness_token= or COMPUTERAGENT_HARNESS_TOKEN)"
        )

    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - httpx ships with [agentos]/[remote]
        raise AgentResolveError("agent_id resolution requires httpx (the [agentos]/[remote] extra)") from exc

    url = f"{base}/agents/resolve"
    try:
        resp = httpx.post(
            url,
            headers={
                "authorization": f"Bearer {bearer}",
                "content-type": "application/json",
                "accept": "application/json",
            },
            json={"agentId": agent_id},
            timeout=timeout,
        )
    except Exception as exc:  # network / DNS / TLS
        raise AgentResolveError(f"agent_id resolution call to {url} failed: {type(exc).__name__}") from exc

    if resp.status_code == 200:
        data: dict[str, Any] = resp.json()
        logger.debug("resolved agent_id %s → name=%s", agent_id, data.get("name"))
        return data
    if resp.status_code == 404:
        return None  # clean miss — caller raises a "not registered" error
    if resp.status_code in (401, 403):
        raise AgentResolveError(
            f"agent_id resolution unauthorized ({resp.status_code}) — the API key's "
            "role/group can't read this agent (needs agents:read + the owning group)"
        )
    raise AgentResolveError(f"agent_id resolution returned {resp.status_code}")


__all__ = ["resolve_agent", "AgentResolveError"]
