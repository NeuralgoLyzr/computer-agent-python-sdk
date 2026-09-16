"""Turn an agent's MCP bindings into in-process servers the engine can call.

The hosted stack materialises each binding as an **HTTP** MCP server pointing at
a bridge (``applyMcpServers``, ``computeragent-server/src/index.ts:4226-4295``),
because its consumer is a CLI in a sandbox that can only be handed a URL. Here
the consumer is ``claude_agent_sdk`` in this same process, which accepts an
in-process server — so each binding becomes one ``create_sdk_mcp_server`` whose
tools call the facade directly.

That difference is not just convenience. It means there is **no listener, no
port, no URL and no inbound auth surface**, so the open-relay hazard that forced
the hosted bridge to drop its operator token (``8a0038c``) is structurally
absent: nothing can call us.

Tool naming matches the hosted stack exactly, because the model and any
allowlist see the same strings either way: server ``<tool_name>``, tools
``mcp__<tool_name>__<action>``, server-level grant ``mcp__<tool_name>``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .content import normalize_content

if TYPE_CHECKING:
    from .binding import McpBinding
    from .client import LyzrMcpClient, ToolSpec

logger = logging.getLogger("computeragent.mcp.server")

#: A namespace has to survive being concatenated into `mcp__<ns>__<tool>` and
#: matched against an allowlist. `tool_name` defaults to the server id, which is
#: always safe — but an operator can set it to anything, and nothing validates
#: it: not AgentOS, not the CAS, and not claude-agent-sdk, which serialises
#: whatever key it is given straight into `--mcp-config`. The TS wire-shape
#: docstring's own example is `"My Server"`.
_SAFE_NAMESPACE = re.compile(r"^[A-Za-z0-9_-]+$")

#: Server names the loader may already have produced. Overwriting either would
#: silently disable the GAP repo's own tools.
RESERVED_NAMESPACES = frozenset({"gap_tools"})


@dataclass
class McpServers:
    """What to merge into the engine options."""

    #: ``{namespace: McpSdkServerConfig}`` — keys must equal the server name for
    #: the ``mcp__<name>__*`` prefix to resolve.
    servers: dict[str, Any] = field(default_factory=dict)
    #: Server-level grants, ``mcp__<namespace>``. Only meaningful when an
    #: ``allowed_tools`` list already exists — see the coordinator's merge.
    grants: list[str] = field(default_factory=list)
    #: Fully-qualified tool names, for logging and tests.
    tool_names: list[str] = field(default_factory=list)
    #: The ``SdkMcpTool`` objects actually built, per namespace. Recorded
    #: because the handlers are otherwise unreachable once they are sealed
    #: inside ``create_sdk_mcp_server``'s dispatch closure — which would make
    #: the per-tool-closure property below untestable through the real build
    #: loop, and that property is exactly a loop-level one.
    tools: dict[str, list[Any]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.servers)


async def build_mcp_servers(bindings: tuple[McpBinding, ...], client: LyzrMcpClient) -> McpServers:
    """Discover each binding's tools and wrap them as in-process MCP servers.

    **Never raises.** A facade outage degrades the run to "no MCP tools this
    turn", which the model can work around; an exception out of boot it cannot.
    This mirrors ``mcpDescriptorsFor``'s explicit never-throws rule
    (``agent-defs.ts:888-891``).
    """
    from claude_agent_sdk import create_sdk_mcp_server

    out = McpServers()
    for binding in bindings:
        namespace = binding.tool_name
        if not _SAFE_NAMESPACE.match(namespace):
            logger.warning(
                "skipping MCP binding %s: tool_name %r is not usable as a tool namespace "
                "(allowed: letters, digits, _ and -). It would be concatenated into "
                "'mcp__%s__<tool>', which no allowlist can match. Fix tool_name on the agent, "
                "or clear it to fall back to the server id.",
                binding.server_id,
                namespace,
                namespace,
            )
            continue
        if namespace in RESERVED_NAMESPACES or namespace in out.servers:
            logger.warning(
                "skipping MCP binding %s: the namespace %r is already taken "
                "(reserved, or claimed by an earlier binding). AgentOS enforces action-name "
                "uniqueness across bindings but NOT tool_name uniqueness, so two bindings can "
                "legitimately arrive with the same one — the first wins rather than being "
                "silently overwritten.",
                binding.server_id,
                namespace,
            )
            continue

        try:
            specs = await client.list_tools(binding.server_id, allowlist=binding.action_names)
        except Exception as exc:
            logger.warning(
                "MCP discovery failed for server %s (%s: %s) — this turn runs without its tools",
                binding.server_id,
                type(exc).__name__,
                exc,
            )
            continue

        if not specs:
            # The single most common cause of a bound tool never appearing, and
            # the two lists side by side are the whole diagnosis.
            await _log_empty_discovery(client, binding)
            continue

        tools = [_build_tool(client, binding, spec) for spec in specs]
        out.servers[namespace] = create_sdk_mcp_server(name=namespace, version="1.0.0", tools=tools)
        out.tools[namespace] = tools
        out.grants.append(f"mcp__{namespace}")
        out.tool_names.extend(f"mcp__{namespace}__{spec.name}" for spec in specs)

    if out.servers:
        logger.info(
            "MCP attached: %s",
            ", ".join(f"{ns}({len(ts)} tools)" for ns, ts in _counts(out).items()),
        )
    return out


def _build_tool(client: LyzrMcpClient, binding: McpBinding, spec: ToolSpec) -> Any:
    """One tool, in its own function.

    The separate function is the point, not style: defining the handler inside
    the loop would close over the loop variable, so every tool would end up
    calling whichever spec the loop finished on. Paid for once already in this
    repo — ``identity/gap_tools.py:140-161`` exists for the same reason.
    """
    from claude_agent_sdk import tool

    allowed = set(binding.action_names)

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        # Re-check the allowlist at CALL time, not only at list time. A model
        # that saw a wider list earlier, or simply guessed, must not reach a
        # tool this agent was never bound to (`mcp.ts:266-272`).
        if allowed and spec.name not in allowed:
            return _error(f"tool not available on this binding: {spec.name}")

        try:
            out = await client.execute(binding.server_id, spec.name, args or {})
        except Exception as exc:
            # An is_error result, not a raise. `run_tool` would convert an
            # exception into one anyway, so raising only loses the message we
            # would have written — and the model can act on a readable failure.
            logger.warning(
                "MCP call failed: %s on %s (%s: %s)",
                spec.name,
                binding.server_id,
                type(exc).__name__,
                exc,
            )
            return _error(f"MCP tool call failed: {exc}")

        # MCP keeps two error channels distinct and the distinction matters: a
        # TOOL that ran and failed is a normal result with is_error, which the
        # model sees and can work around. The facade collapses both into
        # `success:false`; everything reaching here got an HTTP 200, so it is a
        # tool-level outcome (`mcp.ts:309-334`).
        if out.get("success") is False:
            return _error(str(out.get("error") or "tool execution failed"))

        return {"content": normalize_content(out.get("result")), "is_error": False}

    description = spec.description or f"Lyzr MCP tool {spec.name} on {binding.server_id}"
    return tool(spec.name, description, spec.input_schema)(handler)


async def _log_empty_discovery(client: LyzrMcpClient, binding: McpBinding) -> None:
    """Say what was asked for AND what the server actually has."""
    try:
        every = [t.name for t in await client.list_tools(binding.server_id)]
    except Exception:  # noqa: BLE001 - diagnostics only; the caller already skipped
        every = []
    logger.warning(
        "MCP binding %s (%s) matched no tools. Bound action_names: %s. Tools the server actually "
        "exposes: %s. These must match EXACTLY.",
        binding.server_id,
        binding.tool_name,
        ", ".join(binding.action_names) or "(none)",
        ", ".join(every) or "(none, or the list could not be fetched)",
    )


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def _counts(out: McpServers) -> dict[str, list[str]]:
    counts: dict[str, list[str]] = {ns: [] for ns in out.servers}
    for name in out.tool_names:
        namespace = name.split("__")[1]
        counts.setdefault(namespace, []).append(name)
    return counts


__all__ = ["RESERVED_NAMESPACES", "McpServers", "build_mcp_servers"]
