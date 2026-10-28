"""The escape hatch: point at a hosted MCP bridge instead of calling the facade.

A deployment that already fronts this SDK with the CAS's translator proxy has a
bridge running (``llm-proxy-openai/src/mcp.ts``, mounted at ``POST /mcp/<id>``).
Pointing at it skips the in-process client entirely, which is worth one kwarg
for two reasons: a single audited bridge implementation, and a way out if the
facade changes shape and the Python port lags.

The entries built here are byte-for-byte what ``applyMcpServers`` emits
(``computeragent-server/src/index.ts:4270-4285``), because the model, the
allowlist and the bridge all have to see exactly the same strings either way.

**Two things go wrong with this in practice**, both worth knowing before
reaching for it:

- The bridge 401s a request presenting no credential, with no operator-token
  fallback (``mcp.ts:124-136``). The ``x-api-key`` header below is the whole
  authentication story.
- The URL must be the proxy's *externally reachable* origin. The CAS's own
  default is ``http://127.0.0.1:8786``, which is meaningless from outside its
  pod — a deployment that has not set ``LYZR_PROXY_HOST=0.0.0.0`` and
  ``LYZR_PROXY_PUBLIC_URL`` has nothing here to point at.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import quote

from .server import _SAFE_NAMESPACE, RESERVED_NAMESPACES, McpServers

if TYPE_CHECKING:
    from .binding import McpBinding

logger = logging.getLogger("computeragent.mcp.bridge")


def build_bridge_servers(
    bindings: tuple[McpBinding, ...], *, bridge_url: str, api_key: str
) -> McpServers:
    """HTTP MCP server entries pointing at a hosted bridge.

    Synchronous and network-free: unlike the in-process path there is no
    discovery step, because the bridge does ``tools/list`` itself when the CLI
    handshakes. The cost is that a bad binding is invisible until then.
    """
    base = bridge_url.rstrip("/")
    out = McpServers()
    for binding in bindings:
        namespace = binding.tool_name
        # Same guards as the in-process path — the namespace ends up in
        # `mcp__<ns>__<tool>` either way, and nothing upstream validates it.
        if not _SAFE_NAMESPACE.match(namespace):
            logger.warning(
                "skipping MCP binding %s: tool_name %r is not usable as a tool namespace",
                binding.server_id,
                namespace,
            )
            continue
        if namespace in RESERVED_NAMESPACES or namespace in out.servers:
            logger.warning(
                "skipping MCP binding %s: the namespace %r is already taken",
                binding.server_id,
                namespace,
            )
            continue

        # The allowlist rides in the QUERY STRING, which is what keeps the
        # bridge stateless — it holds no per-agent binding of its own
        # (`index.ts:4268-4271`).
        tools = [t for t in binding.action_names if t]
        query = f"?tools={quote(','.join(tools))}" if tools else ""
        out.servers[namespace] = {
            "type": "http",
            "url": f"{base}/mcp/{quote(binding.server_id, safe='')}{query}",
            "headers": {"x-api-key": api_key},
            # Untyped extra key; `subprocess_cli` forwards external-server
            # config verbatim, so it survives into --mcp-config as the CAS
            # sends it. Keeps the tools in the turn-1 prompt.
            "alwaysLoad": True,
        }
        out.grants.append(f"mcp__{namespace}")
        out.tool_names.extend(f"mcp__{namespace}__{t}" for t in tools)

    return out


__all__ = ["build_bridge_servers"]
