"""Surface A — expose the harness's supervised fan-out to the orchestrator LLM
as an in-process ``dispatch_many`` tool.

The orchestrator asks for N workers; they run as their own concurrent sessions;
the reports come back as the tool result. Only injected when the harness gives
the engine a dispatch hook.

Tool name as seen by the model: ``mcp__orchestration__dispatch_many``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

# Re-exported from the contract module rather than declared here: the harness
# policy gate has to recognise this exact name too, and two copies would drift.
from .contract import (  # noqa: F401
    ORCHESTRATION_SUPERVISED_DISPATCH_MANY_TOOL_NAME,
    DispatchRequestItem,
    DispatchResultItem,
    WorkerScope,
)

logger = logging.getLogger("computeragent.orchestration.dispatch_tool")

DISPATCH_SERVER_NAME = "orchestration"

DispatchHook = Callable[[Sequence[DispatchRequestItem]], Awaitable[Sequence[DispatchResultItem]]]

_WORKER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "What this worker must do (required).",
        },
        "id": {"type": "string", "description": "Stable instance id, e.g. 'w1'."},
        "use": {
            "type": "string",
            "description": "Name of a declared crew worker to instantiate.",
        },
        "role": {
            "type": "string",
            "description": "Inline worker role (shared-codebase mode).",
        },
        "scope": {
            "type": "object",
            "description": "Filesystem scope this worker may write.",
            "properties": {
                "write": {"type": "array", "items": {"type": "string"}},
                "read": {"type": "array", "items": {"type": "string"}},
            },
        },
        "model": {"type": "string"},
        "tools": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["task"],
}


def _scope_of(raw: Any) -> WorkerScope | None:
    if not isinstance(raw, dict):
        return None
    write = (
        tuple(x for x in raw["write"] if isinstance(x, str))
        if isinstance(raw.get("write"), list)
        else None
    )
    read = (
        tuple(x for x in raw["read"] if isinstance(x, str))
        if isinstance(raw.get("read"), list)
        else None
    )
    if write is None and read is None:
        return None
    return WorkerScope(write=write, read=read)


def _to_request_item(raw: Any) -> DispatchRequestItem | None:
    if not isinstance(raw, dict):
        return None
    task = raw.get("task")
    if not isinstance(task, str) or not task:
        return None
    tools = (
        tuple(t for t in raw["tools"] if isinstance(t, str))
        if isinstance(raw.get("tools"), list)
        else None
    )
    return DispatchRequestItem(
        task=task,
        id=raw["id"] if isinstance(raw.get("id"), str) else None,
        use=raw["use"] if isinstance(raw.get("use"), str) else None,
        role=raw["role"] if isinstance(raw.get("role"), str) else None,
        model=raw["model"] if isinstance(raw.get("model"), str) else None,
        tools=tools,
        scope=_scope_of(raw.get("scope")),
    )


def build_dispatch_server(dispatch: DispatchHook) -> Any:
    """The in-process ``orchestration`` MCP server carrying ``dispatch_many``."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        raw_workers = (args or {}).get("workers")
        items = [
            item for item in (_to_request_item(w) for w in raw_workers or []) if item is not None
        ]
        if not items:
            # An is_error result, not a raise: the model can read this and try
            # again with a usable roster, where an exception would only end the
            # turn.
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "dispatch_many needs at least one worker, each with a "
                        "non-empty `task`.",
                    }
                ],
                "is_error": True,
            }
        results = await dispatch(items)
        payload = [
            {
                "id": r.id,
                "status": r.status,
                "text": r.text,
                **({"error": r.error} if r.error else {}),
            }
            for r in results
        ]
        return {
            "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
            "is_error": False,
        }

    built = tool(
        "dispatch_many",
        "Spawn worker sub-agents to run CONCURRENTLY, each as its own isolated session. "
        "Pass an array of workers (each needs a `task`). Returns each worker's report.",
        {
            "type": "object",
            "properties": {"workers": {"type": "array", "items": _WORKER_SCHEMA}},
            "required": ["workers"],
        },
    )(handler)

    return create_sdk_mcp_server(name=DISPATCH_SERVER_NAME, version="1.0.0", tools=[built])


__all__ = [
    "DISPATCH_SERVER_NAME",
    "ORCHESTRATION_SUPERVISED_DISPATCH_MANY_TOOL_NAME",
    "DispatchHook",
    "build_dispatch_server",
]
