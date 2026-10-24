"""Org (global) RAI policies — ``GET {agent_api_url}/v3/org/rai-policy``.

A port of CAS ``harness-server/src/services/org-rai-policy.ts``. An org can
mandate several RAI policies that apply to every agent in it, on top of the
agent's own. They are read with the caller's key (the org is derived from the
key upstream) at the start of every turn, with no cache, so an admin's change
applies to the next turn.

Response shapes (keys are omitted, not nulled, in the empty case)::

    list     {org_id, policy_id, enabled, …, policies: [{policy_id, enabled, fail_open, timeout, priority, policy_exists, …}]}
    legacy   {org_id, policy_id, enabled, fail_open, timeout, policy_exists, policy_name, …}
    none     {org_id, enabled: false, policy_id: null[, policies: []]}

When ``policies`` is a list it is authoritative (already priority-sorted
upstream); otherwise the legacy single fields are read. ``enabled: false`` rows
still carry a ``policy_id``, so a row counts only with ``enabled`` true AND an
id. ``400`` and ``403`` mean the caller has no org context: no org policy, not
an outage. Every other failure raises, because the policies are mandatory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("computeragent.policy.org")

#: The API path is fixed; only the base is configurable.
ORG_RAI_POLICY_PATH = "/v3/org/rai-policy"

#: Bounds the read so a hung agent API cannot stall the turn.
REQUEST_TIMEOUT_S = 8.0

#: lyzr-agent's default for an org policy with no ``timeout`` (seconds).
DEFAULT_ORG_TIMEOUT_S = 100.0


class OrgPolicyFetchError(RuntimeError):
    """The org policies could not be read. Never carries the key or a body."""


@dataclass(frozen=True)
class OrgRaiPolicy:
    """One org RAI policy that applies to this turn."""

    policy_id: str
    #: On an SRS no-verdict: ``True`` lets the check pass, ``False`` blocks.
    fail_open: bool
    #: The org row's ``timeout`` in seconds; 100 when unset.
    timeout_s: float
    #: ``policy_exists: false`` — deleted in SRS after it was designated. The id
    #: is still sent (SRS 404s on it and the check fails closed); the flag makes
    #: that outcome attributable instead of looking like an outage.
    dangling: bool


def _timeout_of(raw: Any) -> float:
    """The row's ``timeout`` (seconds, any positive number), else 100s."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_ORG_TIMEOUT_S
    return value if value > 0 and value != float("inf") else DEFAULT_ORG_TIMEOUT_S


def _policy_of(row: dict[str, Any]) -> OrgRaiPolicy | None:
    policy_id = row.get("policy_id")
    policy_id = policy_id.strip() if isinstance(policy_id, str) else ""
    if row.get("enabled") is not True or not policy_id:
        return None
    return OrgRaiPolicy(
        policy_id=policy_id,
        fail_open=row.get("fail_open") is True,
        timeout_s=_timeout_of(row.get("timeout")),
        dangling=row.get("policy_exists") is False,
    )


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or ''}{f':{parts.port}' if parts.port else ''}"


async def fetch_org_rai_policies(
    agent_api_url: str,
    api_key: str,
    *,
    client: Any | None = None,
) -> list[OrgRaiPolicy]:
    """Every org RAI policy that applies to the holder of ``api_key``, in the
    org's priority order; empty when none applies.

    Raises :class:`OrgPolicyFetchError` when they cannot be read.
    """
    if not api_key:
        return []

    import httpx

    url = f"{agent_api_url.rstrip('/')}{ORG_RAI_POLICY_PATH}"
    host = _origin(url)
    try:
        if client is not None:
            resp = await client.get(url, headers={"x-api-key": api_key}, timeout=REQUEST_TIMEOUT_S)
        else:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as owned:
                resp = await owned.get(url, headers={"x-api-key": api_key})
    except Exception as exc:  # network / DNS / TLS / timeout
        logger.warning("org policy fetch failed: %s unreachable (%s)", host, type(exc).__name__)
        raise OrgPolicyFetchError(
            f"could not read the org RAI policies from {host} ({type(exc).__name__})"
        ) from None

    if resp.status_code in (400, 403):
        return []
    if resp.status_code >= 300:
        # The size, never the text: an error body can echo the request.
        logger.warning(
            "org policy fetch failed: %s returned %s (%s bytes)",
            host,
            resp.status_code,
            len(resp.content),
        )
        raise OrgPolicyFetchError(f"the org RAI policy API at {host} returned {resp.status_code}")

    try:
        body = resp.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        logger.warning("org policy fetch failed: %s returned malformed JSON", host)
        raise OrgPolicyFetchError(f"the org RAI policy API at {host} returned malformed JSON")

    entries = body["policies"] if isinstance(body.get("policies"), list) else [body]
    policies: list[OrgRaiPolicy] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        policy = _policy_of(entry)
        if policy is None:
            continue
        if policy.dangling:
            logger.warning(
                "org RAI policy %s no longer exists in SRS; checks this turn fail closed unless "
                "every org policy sets fail_open",
                policy.policy_id,
            )
        policies.append(policy)
    return policies


__all__ = [
    "DEFAULT_ORG_TIMEOUT_S",
    "ORG_RAI_POLICY_PATH",
    "OrgPolicyFetchError",
    "OrgRaiPolicy",
    "fetch_org_rai_policies",
]
