"""Fetch a group-scoped git PAT from the AgentOS server to clone private GAP
repos.

The SDK already holds an AgentOS API key (``cak_…``) — the same one used for the
remote coordinator / telemetry ingest. We present it as a bearer token to
``POST {AGENTOS_API_URL}/git-credentials/resolve`` with the repo URL; the server
returns the decrypted PAT for the key's group + the repo host (or 404 if none).

Best-effort by design: any missing config, network error, 401, or 404 returns
``None`` so the caller falls back to an unauthenticated clone (unchanged
public-repo behavior). The token is NEVER logged.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("computeragent.git_credentials")


def _api_base(explicit: str | None) -> str | None:
    if explicit:
        return explicit.rstrip("/")
    from .. import agentos

    return agentos.api_url()


def _token(explicit: str | None) -> str | None:
    from .. import agentos

    return explicit or agentos.cak()


async def resolve_git_credential(
    repo_url: str,
    *,
    api_base: str | None = None,
    token: str | None = None,
    timeout: float = 10.0,
) -> dict[str, str] | None:
    """Resolve ``{host, username, token}`` for ``repo_url``, or ``None``.

    Returns ``None`` (caller falls back to unauthenticated clone) when AgentOS
    isn't configured, the request fails, or no credential matches.
    """
    base = _api_base(api_base)
    bearer = _token(token)
    if not base or not bearer:
        return None

    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx ships with the [agentos] extra
        logger.debug("httpx not installed; skipping git credential resolve")
        return None

    url = f"{base}/git-credentials/resolve"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                headers={
                    "authorization": f"Bearer {bearer}",
                    "content-type": "application/json",
                    "accept": "application/json",
                },
                json={"repoUrl": repo_url},
            )
    except Exception as exc:  # network / DNS / TLS — never fatal
        logger.debug("git credential resolve failed (%s); cloning unauthenticated", type(exc).__name__)
        return None

    if resp.status_code == 200:
        data: dict[str, Any] = resp.json()
        tok = data.get("token")
        if not tok:
            return None
        # Do not log the token.
        logger.debug("resolved git credential for host %s", data.get("host"))
        return {
            "host": str(data.get("host", "")),
            "username": str(data.get("username") or "x-access-token"),
            "token": str(tok),
        }
    # 404 (no credential), 401 (bad/revoked key), 403 (missing permission) → fall back.
    logger.debug("git credential resolve returned %s; cloning unauthenticated", resp.status_code)
    return None


__all__ = ["resolve_git_credential"]
