"""Single-config AgentOS connectivity via a discovery document.

A consumer gives the SDK exactly ONE AgentOS URL — ``AGENTOS_DISCOVERY_URL``,
the full URL of the server's discovery endpoint — plus its ``cak_`` key. The SDK
GETs that document once and reads the **absolute URLs** for ingest / api / otel
straight out of it. No endpoint *path* is ever hardcoded or constructed in the
SDK: when the server moves a route it updates its discovery document, and SDKs
pick the new URLs up on their next start — **no SDK release required**.

Resolution order for each endpoint (first hit wins):

1. an explicit full-URL override env var (``AGENTOS_INGEST_URL`` /
   ``AGENTOS_API_URL`` / ``AGENTOS_OTEL_URL``) — a back-compat escape hatch;
   these are full URLs supplied by config, so they're not SDK-baked paths; then
2. the discovery document at ``AGENTOS_DISCOVERY_URL``.

There is **no baked-in path fallback** — if neither is available the endpoint is
simply unconfigured (the relevant sink/client stays off, or raises a clear error
where a value is mandatory). The discovery GET is **memoized once per process**.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger("computeragent.agentos")

# Memoized discovery result. ``_endpoints`` is the parsed map on success; we only
# cache success, so a transient failure is retried on the next call.
_lock = threading.Lock()
_endpoints: dict[str, str] | None = None


def cak() -> str | None:
    """The one AgentOS API key (``cak_…``) the SDK presents everywhere."""
    return os.environ.get("COMPUTERAGENT_HARNESS_TOKEN") or os.environ.get("AGENTOS_INGEST_TOKEN")


def harness_url() -> str | None:
    """Env fallback for the remote-harness URL (the ``harness_url=`` kwarg wins).

    The harness/CAS is a separate service from AgentOS — it's a full URL supplied
    by config, never discovered (AgentOS only knows its loopback ``CA_BASE``,
    which isn't reachable by an external SDK)."""
    base = os.environ.get("COMPUTERAGENT_HARNESS_URL")
    return base.rstrip("/") if base else None


def _discover(*, timeout: float = 5.0) -> dict[str, str]:
    """Fetch + memoize the discovery document's ``endpoints`` map.

    Returns ``{}`` when ``AGENTOS_DISCOVERY_URL`` is unset or the fetch fails —
    callers treat an empty/missing key as "not configured". Only successful
    lookups are cached, so a brief outage is retried later.
    """
    global _endpoints
    if _endpoints is not None:
        return _endpoints

    url = os.environ.get("AGENTOS_DISCOVERY_URL")
    if not url:
        return {}

    with _lock:
        if _endpoints is not None:  # filled while we waited on the lock
            return _endpoints
        try:
            import httpx
        except ImportError:  # pragma: no cover - httpx ships with [agentos]/[remote]
            logger.debug("httpx not installed; cannot fetch AgentOS discovery")
            return {}

        headers = {"accept": "application/json"}
        key = cak()
        if key:  # discovery is public, but send the key in case it's ever gated
            headers["authorization"] = f"Bearer {key}"
        try:
            resp = httpx.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            doc: dict[str, Any] = resp.json()
        except Exception as exc:  # network / DNS / TLS / bad JSON — never fatal
            logger.warning(
                "AgentOS discovery fetch from %s failed (%s); AgentOS endpoints unconfigured",
                url,
                type(exc).__name__,
            )
            return {}

        endpoints = doc.get("endpoints")
        if not isinstance(endpoints, dict):
            logger.warning("AgentOS discovery doc from %s has no 'endpoints' map", url)
            return {}
        parsed = {str(k): str(v) for k, v in endpoints.items() if isinstance(v, str)}
        _endpoints = parsed
        logger.debug("AgentOS discovery resolved %s endpoint(s) from %s", len(parsed), url)
        return parsed


def _resolve(endpoint: str, override_env: str) -> str | None:
    """Explicit full-URL override env var → discovery → ``None``."""
    explicit = os.environ.get(override_env)
    if explicit:
        return explicit.rstrip("/")
    value = _discover().get(endpoint)
    return value.rstrip("/") if value else None


def ingest_url() -> str | None:
    """Telemetry ingest URL (``AGENTOS_INGEST_URL`` override → discovery)."""
    return _resolve("ingest", "AGENTOS_INGEST_URL")


def api_url() -> str | None:
    """Dashboard v1 API base — agent resolve + git-credentials
    (``AGENTOS_API_URL`` override → discovery)."""
    return _resolve("api", "AGENTOS_API_URL")


def otel_url() -> str | None:
    """OTLP trace-gateway base (``AGENTOS_OTEL_URL`` override → discovery)."""
    return _resolve("otel", "AGENTOS_OTEL_URL")


def policy_config_url(name: str) -> str | None:
    """AgentOS ingest ``policy-config/{name}`` URL, derived from the ingest
    endpoint (``…/ingest/events`` → ``…/ingest/policy-config/{name}``).

    ``None`` when no ingest endpoint is configured."""
    base = ingest_url()
    if not base:
        return None
    from urllib.parse import quote

    root = base[: -len("/events")] if base.endswith("/events") else base
    return f"{root}/policy-config/{quote(name, safe='')}"


def fetch_policy_binding(
    name: str, *, token: str | None = None, timeout: float = 5.0
) -> dict[str, str] | None:
    """Fetch the SRS policy *binding* for an agent name from AgentOS.

    Returns ``{"kind": "srs", "policyId": …, "principalId": …}`` when the agent
    has a bound policy, else ``None``. Never returns the SRS endpoint/key — those
    stay server-side; the CAS fills them from its own env. Best-effort: any
    network/config error logs and returns ``None`` (no policy rather than a crash).
    """
    url = policy_config_url(name)
    if not url:
        return None
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx ships with [remote]/[agentos]
        logger.debug("httpx not installed; cannot fetch AgentOS policy-config")
        return None

    headers = {"accept": "application/json"}
    key = token or cak()
    if key:
        headers["authorization"] = f"Bearer {key}"
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        doc = resp.json()
    except Exception as exc:  # network / DNS / TLS / bad JSON — never fatal
        logger.warning("AgentOS policy-config fetch from %s failed (%s)", url, exc)
        return None

    policy = doc.get("policy") if isinstance(doc, dict) else None
    if not isinstance(policy, dict) or not policy.get("policyId"):
        return None
    return {
        "kind": "srs",
        "policyId": str(policy["policyId"]),
        "principalId": str(policy.get("principalId") or name),
    }


def bind_policy(
    agent_id: str, policy_id: str | None, *, token: str | None = None, timeout: float = 10.0
) -> dict[str, str] | None:
    """Attach (or detach) an SRS policy to an agent — same write the AgentOS UI's
    Policy tab performs (``PUT /agents/{id}/policy``).

    ``agent_id`` is the registry id from the dashboard URL; ``policy_id`` is the
    policy ``_id`` from the Policies page (``None`` detaches). Persistent: future
    runs of the agent (UI and SDK alike) pick the binding up automatically.
    Returns the resulting binding (``{"policyId": …}``) or ``None`` when detached.
    Raises ``RuntimeError`` when AgentOS is unreachable/unconfigured — a silent
    no-op here would masquerade as an attached policy.
    """
    base = api_url()
    key = token or cak()
    if not base or not key:
        raise RuntimeError(
            "bind_policy needs AgentOS — set AGENTOS_DISCOVERY_URL (or AGENTOS_API_URL) "
            "and a cak_ key (COMPUTERAGENT_HARNESS_TOKEN)."
        )
    from urllib.parse import quote

    import httpx

    resp = httpx.put(
        f"{base}/agents/{quote(agent_id, safe='')}/policy",
        headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
        json={"policy_id": policy_id},
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"bind_policy: AgentOS returned {resp.status_code}: {resp.text[:200]}")
    doc = resp.json()
    binding = doc.get("binding") if isinstance(doc, dict) else None
    return binding if isinstance(binding, dict) else None


def _reset_cache_for_tests() -> None:
    """Clear the memoized discovery result (test-only)."""
    global _endpoints
    with _lock:
        _endpoints = None


__all__ = [
    "cak",
    "harness_url",
    "ingest_url",
    "api_url",
    "otel_url",
]
