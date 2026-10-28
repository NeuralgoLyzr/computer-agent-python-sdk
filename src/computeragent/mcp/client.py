"""Client for the Lyzr MCP service's REST facade.

The facade is not the MCP wire protocol — it is two plain HTTP routes::

    GET  {base}/api/v1/mcp/servers/{id}/tools   → {"tools": [{name, description, input_schema}]}
    POST {base}/api/v1/mcp/tools/execute        → {"result": …, "success": bool, "error": str}

In the hosted stack a bridge inside the translator proxy speaks real MCP on top
of these (``llm-proxy-openai/src/mcp.ts``). This SDK needs no bridge: its
consumer is in-process. So only the translation half is ported, and this module
is that half's transport.

**Credential.** The caller's own Lyzr key, forwarded as ``x-api-key``
(``mcp.ts:352-355``) — the same key that already fetched the agent document.
There is no operator token: the hosted bridge dropped its own in ``8a0038c``
precisely because a credential the service supplies makes it an open relay.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

logger = logging.getLogger("computeragent.mcp.client")

#: ``tools/list`` is a live round-trip from the Lyzr service to the REMOTE MCP
#: server, and the CLI asks for it on every session handshake. Mirrors
#: ``DEFAULT_TOOLS_TTL_MS`` (``mcp.ts:47``).
DEFAULT_TOOLS_TTL_S = 60.0

#: What a well-formed JSON Schema object looks like to
#: ``claude_agent_sdk._build_input_schema``: a ``type`` string AND ``properties``.
#: Anything else it reinterprets as a ``{param: python_type}`` mapping, after
#: which every call fails argument validation before the handler ever runs.
_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


class McpServiceError(RuntimeError):
    """The facade could not be reached, or answered with a non-2xx.

    Never carries the credential — the message is shown to users and logged.
    """


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


class LyzrMcpClient:
    """Two calls against the facade, with the bridge's caching semantics."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        tools_ttl_s: float = DEFAULT_TOOLS_TTL_S,
        client: Any | None = None,
        now: Any = time.monotonic,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._ttl = tools_ttl_s
        self._client = client
        self._now = now
        self._cache: dict[str, tuple[float, list[ToolSpec]]] = {}

    def __repr__(self) -> str:  # pragma: no cover - trivial, but the point is the omission
        """No credential, ever. This object reaches tracebacks and debug logs."""
        return f"<LyzrMcpClient base={self._base!r}>"

    async def list_tools(
        self, server_id: str, *, allowlist: tuple[str, ...] = ()
    ) -> list[ToolSpec]:
        """The server's tools, filtered to ``allowlist`` when one is given.

        Cached for ``tools_ttl_s``, keyed on ``server_id`` **and the credential**:
        what a server exposes can differ per caller (``persist_auth``, per-user
        OAuth), so one caller's list must not be served to another
        (``mcp.ts:214-216``).
        """
        key = f"{server_id}::{self._api_key}"
        hit = self._cache.get(key)
        now = self._now()
        if hit is not None and hit[0] > now:
            tools = hit[1]
            # Worth saying out loud: a stale cache is why a tool added to the
            # MCP server minutes ago is still invisible to the agent.
            logger.debug("tools/list cache hit server=%s (%.0fs left)", server_id, hit[0] - now)
        else:
            raw = await self._get(f"/api/v1/mcp/servers/{quote(server_id, safe='')}/tools")
            tools = [_spec(t) for t in (raw.get("tools") or []) if isinstance(t, dict)]
            self._cache[key] = (now + self._ttl, tools)

        # Filter AFTER caching, so one agent's narrow binding cannot poison the
        # cache for another agent bound to the same server with a wider one
        # (`mcp.ts:243-247`).
        if not allowlist:
            return list(tools)
        allowed = set(allowlist)
        return [t for t in tools if t.name in allowed]

    async def execute(
        self, server_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Run one tool. The envelope is returned as-is — the ``success`` flag is
        a *tool-level* outcome and belongs to the caller to interpret."""
        return await self._post(
            "/api/v1/mcp/tools/execute",
            {"server_id": server_id, "tool_name": tool_name, "arguments": arguments},
        )

    # ── transport ────────────────────────────────────────────────────────────

    @property
    def _headers(self) -> dict[str, str]:
        # `x-api-key` only. The facade also accepts Bearer, but sending both
        # would put the credential in two places for no gain.
        return {"x-api-key": self._api_key, "content-type": "application/json"}

    async def _get(self, path: str) -> dict[str, Any]:
        return await self._request("GET", path, None)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", path, body)

    async def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        import httpx

        url = f"{self._base}{path}"
        try:
            if self._client is not None:
                resp = await self._client.request(
                    method, url, json=body, headers=self._headers, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as http:
                    resp = await http.request(method, url, json=body, headers=self._headers)
        except Exception as exc:
            # Type name only — an httpx message can carry the URL, and a URL can
            # carry a query string someone put a token in.
            raise McpServiceError(
                f"could not reach the Lyzr MCP service at {url} ({type(exc).__name__})"
            ) from None

        status = getattr(resp, "status_code", 0)
        if status >= 400:
            raise McpServiceError(f"{url} returned HTTP {status}: {_snippet(resp)}")
        try:
            parsed = resp.json()
        except Exception as exc:
            raise McpServiceError(f"{url} returned a non-JSON body") from exc
        return parsed if isinstance(parsed, dict) else {}


def _spec(raw: dict[str, Any]) -> ToolSpec:
    """One facade tool → a spec the SDK's ``tool()`` decorator accepts.

    The facade already renamed MCP's ``inputSchema`` to snake_case
    (``mcp.ts:235-239``); we hand the schema on verbatim when it is well-formed,
    because it is what the remote server declares and therefore what the
    arguments will actually be validated against.
    """
    name = str(raw.get("name") or "")
    description = raw.get("description")
    return ToolSpec(
        name=name,
        description=str(description) if isinstance(description, str) and description else "",
        input_schema=_usable_schema(raw.get("input_schema")),
    )


def _usable_schema(raw: Any) -> dict[str, Any]:
    """Pass a schema through only if ``_build_input_schema`` will recognise it.

    Upstream keeps a dict verbatim only when it has a string ``type`` *and* a
    ``properties`` key. Anything else — ``{"type": "object"}`` with no
    properties, a bare ``{}``, a list — it reads as a ``{param: type}`` mapping
    and rebuilds into a schema with the wrong required fields. Every call then
    fails jsonschema validation before the handler runs, which presents as a
    tool that is visible and permanently broken.
    """
    if not isinstance(raw, dict):
        return dict(_EMPTY_SCHEMA)
    if isinstance(raw.get("type"), str) and "properties" in raw:
        return raw
    if not raw:
        return dict(_EMPTY_SCHEMA)
    logger.warning(
        "tool input_schema is not a usable JSON Schema object (keys: %s) — falling back to an "
        "open schema so the tool stays callable; arguments will not be validated locally",
        ", ".join(sorted(map(str, raw))) or "none",
    )
    return dict(_EMPTY_SCHEMA)


def _snippet(resp: Any) -> str:
    try:
        return str(getattr(resp, "text", ""))[:300]
    except Exception:  # pragma: no cover - defensive
        return "<unreadable body>"


__all__ = [
    "DEFAULT_TOOLS_TTL_S",
    "LyzrMcpClient",
    "McpServiceError",
    "ToolSpec",
]
