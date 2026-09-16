"""MCP support: bind an agent's Lyzr MCP servers as in-process engine tools.

The Lyzr MCP service exposes a REST *facade* over MCP, not the MCP wire
protocol. In the hosted stack a bridge inside the translator proxy speaks real
MCP Streamable HTTP and translates. This SDK has no proxy in front of it, and
does not need one: its consumer is ``claude_agent_sdk`` in the same process,
which accepts an in-process server. So the JSON-RPC half of that bridge
disappears here and only the translation half is ported — no listener, no port,
no inbound auth surface at all.

Note the package name shadows nothing: ``import mcp`` inside these modules
resolves to the upstream ``mcp`` distribution, not to this package, because
Python 3 imports are absolute.
"""

from __future__ import annotations

from .binding import (
    DEFAULT_MCP_MODE,
    MCP_MODES,
    McpBinding,
    McpBindingError,
    parse_mcp_bindings,
    parse_mcp_mode,
)

__all__ = [
    "DEFAULT_MCP_MODE",
    "MCP_MODES",
    "McpBinding",
    "McpBindingError",
    "parse_mcp_bindings",
    "parse_mcp_mode",
]
