"""MCP servers bound to the agent in AgentOS, served through ``mcp_url``.

The bindings come from the agent document's ``mcp.servers``
(``GET {base_url}/v1/agents/{id}``), which AgentOS already resolves for the
caller: a "require user sign-in" binding (``persist_auth: false``) carries the
caller's own server as ``your_server_id``. Every binding is attached to the
engine. This mirrors CAS ``applyMcpServers`` (computeragent-server
``index.ts``):

* The server name, and so the tool namespace ``mcp__<name>__<tool>``, is the
  binding's ``tool_name``, whoever runs the agent.
* Each server is ``{mcp_url}/{name}``, ``{name}`` the binding's ``tool_name``
  percent-encoded as one segment (AgentOS keeps it printable ASCII), with no
  query; behind the egress proxy ``mcp_url`` is ``http://egress/mcp/v1``. The
  agent gateway takes the server id and the allowed tools from its registry.
* A sign-in binding with no ``your_server_id`` is withheld for this caller
  rather than sent to a server they cannot use.
* ``allowed_tools`` gets ``mcp__<name>`` for Anthropic-shaped engines only:
  gitclaw treats ``allowedTools`` as a strict allowlist and namespaces MCP
  tools differently, so the entry would filter out every tool it has.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

logger = logging.getLogger("computeragent.harness.mcp")


class McpBindingError(ValueError):
    """A binding in the agent document cannot be turned into a server."""


@dataclass(frozen=True)
class McpBinding:
    """One ``mcp.servers`` entry of the agent document."""

    server_id: str
    tool_name: str
    action_names: tuple[str, ...] = ()
    persist_auth: bool = True
    #: The caller's own server for a sign-in binding, ``None`` when there is
    #: nothing to swap or they have not connected one.
    your_server_id: str | None = None


def parse_bindings(section: Any) -> tuple[McpBinding, ...]:
    """Parse the document's ``mcp`` section (``{provider, servers: [...]}``).

    A null or absent section, or null ``servers``, means no bindings. A
    malformed entry raises naming its index rather than being dropped: a
    silently missing tool looks like the agent misbehaving. ``provider``,
    ``needs_your_connection`` and ``standard_server_id`` are not read.
    """
    if section is None:
        return ()
    if not isinstance(section, dict):
        raise McpBindingError("mcp must be an object")
    raw = section.get("servers")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise McpBindingError("mcp.servers must be a list")
    bindings: list[McpBinding] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise McpBindingError(f"mcp.servers[{index}] must be an object")
        server_id = entry.get("server_id")
        tool_name = entry.get("tool_name")
        if not (isinstance(server_id, str) and server_id.strip()):
            raise McpBindingError(f"mcp.servers[{index}] has no server_id")
        if not (isinstance(tool_name, str) and tool_name.strip()):
            raise McpBindingError(f"mcp.servers[{index}] ({server_id}) has no tool_name")
        actions = entry.get("action_names") or []
        your_server_id = entry.get("your_server_id")
        bindings.append(
            McpBinding(
                server_id=server_id,
                tool_name=tool_name,
                action_names=tuple(a for a in actions if isinstance(a, str) and a),
                persist_auth=entry.get("persist_auth") is not False,
                your_server_id=your_server_id if isinstance(your_server_id, str) else None,
            )
        )
    return tuple(bindings)


def build_mcp_servers(
    bindings: tuple[McpBinding, ...],
    *,
    mcp_url: str,
    api_key: str,
    anthropic_shaped: bool,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Engine ``mcp_servers`` entries and the ``allowed_tools`` grants for them."""
    base = mcp_url.rstrip("/")
    servers: dict[str, dict[str, Any]] = {}
    allowed: list[str] = []
    for binding in bindings:
        if not binding.persist_auth and binding.your_server_id is None:
            logger.warning(
                "MCP tool %r withheld: it requires the user's own connection and the caller "
                "has not connected one",
                binding.tool_name,
            )
            continue
        url = f"{base}/{quote(binding.tool_name, safe='')}"
        servers[binding.tool_name] = {
            "type": "http",
            "url": url,
            "headers": {"x-api-key": api_key},
            # Keep the tools in the turn-1 prompt rather than behind tool
            # search: the action list already bounds how many there are.
            "alwaysLoad": True,
        }
        if anthropic_shaped:
            allowed.append(f"mcp__{binding.tool_name}")
    return servers, allowed


__all__ = [
    "McpBinding",
    "McpBindingError",
    "build_mcp_servers",
    "parse_bindings",
]
