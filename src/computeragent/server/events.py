"""The harness event stream: CAS's ``sse-events.ts`` events from a ComputerAgent turn.

Decision D23 in ``.claude/plans/p1-sdk-egress-routes.md``. The reference is
``computeragent-enterprise/packages/harness-server`` (``session.ts``,
``services/run-session.ts``):

* ``ca_session_started`` once, when the session is created;
* per turn: ``ca_turn_started`` (the message as the engine receives it, after
  the input guardrail), ``ca_redaction_decision`` (the input guardrail's
  outcome), then ``sdk_message`` for each engine message in the shape of the
  TS Claude Agent SDK's messages, ``ca_permission_request`` /
  ``ca_permission_decision`` around tool calls, and ``ca_usage_snapshot``;
  the turn ends with an ``sdk_message`` whose payload is ``type: "result"``
  (a blocked message too, marked ``ca_policy_blocked``);
* ``ca_session_ended`` once, when the session ends: input closed, cancel,
  delete or an engine error.

A ``ca_usage_snapshot`` is sent before the turn's ``result``, because the CAS
client stops reading a turn at its ``result``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

#: Events kept per session for ``Last-Event-ID`` replay.
REPLAY_LIMIT = 10_000


def _json_safe(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(json.dumps(value, default=str))
    return out


def _block(block: Any) -> Any:
    """One content block as the TS SDK sends it."""
    if isinstance(block, dict):
        return block
    name = type(block).__name__
    if name == "TextBlock":
        return {"type": "text", "text": block.text}
    if name == "ThinkingBlock":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if name == "ToolUseBlock":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if name == "ToolResultBlock":
        out: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
        }
        if block.is_error is not None:
            out["is_error"] = block.is_error
        return out
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    if dataclasses.is_dataclass(block) and not isinstance(block, type):
        return dataclasses.asdict(block)
    return {"type": "unknown", "repr": repr(block)[:500]}


def _content(content: Any) -> Any:
    if isinstance(content, list):
        return [_block(b) for b in content]
    return content


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def to_wire(message: Any) -> dict[str, Any]:
    """An engine message as the TS Claude Agent SDK's JSON message.

    Claude engine messages are ``claude_agent_sdk.types`` dataclasses;
    gitagent's are Anthropic/OpenAI response objects and plain dicts, mapped
    onto ``assistant`` / ``user`` messages.
    """
    name = type(message).__name__
    if name == "AssistantMessage":
        return _json_safe(
            _drop_none(
                {
                    "type": "assistant",
                    "message": _drop_none(
                        {
                            "role": "assistant",
                            "model": message.model,
                            "content": _content(message.content),
                            "id": getattr(message, "message_id", None),
                            "stop_reason": getattr(message, "stop_reason", None),
                            "usage": getattr(message, "usage", None),
                        }
                    ),
                    "parent_tool_use_id": message.parent_tool_use_id,
                    "session_id": getattr(message, "session_id", None),
                    "uuid": getattr(message, "uuid", None),
                    "error": getattr(message, "error", None),
                }
            )
        )
    if name == "UserMessage":
        return _json_safe(
            _drop_none(
                {
                    "type": "user",
                    "message": {"role": "user", "content": _content(message.content)},
                    "parent_tool_use_id": message.parent_tool_use_id,
                    "uuid": getattr(message, "uuid", None),
                    "tool_use_result": getattr(message, "tool_use_result", None),
                }
            )
        )
    if name == "SystemMessage":
        return _json_safe(
            {"type": "system", "subtype": message.subtype, **dict(message.data or {})}
        )
    if name == "ResultMessage":
        fields = dataclasses.asdict(message)
        return _json_safe({"type": "result", **_drop_none(fields)})
    if name == "StreamEvent":
        return _json_safe(
            _drop_none(
                {
                    "type": "stream_event",
                    "uuid": message.uuid,
                    "session_id": message.session_id,
                    "event": message.event,
                    "parent_tool_use_id": message.parent_tool_use_id,
                }
            )
        )
    if isinstance(message, dict):
        if message.get("role") == "tool":
            return _json_safe(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.get("tool_call_id", ""),
                                "content": message.get("content"),
                            }
                        ],
                    },
                }
            )
        if "type" in message:
            return _json_safe(message)
        return _json_safe({"type": "unknown", "payload": message})
    if hasattr(message, "model_dump"):
        dumped = message.model_dump(mode="json", exclude_none=True)
        if dumped.get("type") == "message" and dumped.get("role") == "assistant":  # Anthropic
            return {"type": "assistant", "message": dumped}
        choices = dumped.get("choices")
        if isinstance(choices, list) and choices:  # OpenAI chat completion
            msg = choices[0].get("message") or {}
            content: list[dict[str, Any]] = []
            if msg.get("content"):
                content.append({"type": "text", "text": msg["content"]})
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {"raw": fn.get("arguments")}
                content.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id"),
                        "name": fn.get("name"),
                        "input": args,
                    }
                )
            return {
                "type": "assistant",
                "message": {"role": "assistant", "model": dumped.get("model"), "content": content},
            }
        return {"type": "unknown", "payload": dumped}
    return {"type": "unknown", "repr": repr(message)[:500]}


def assistant_text(payload: dict[str, Any]) -> str:
    """The text of an ``assistant`` wire message, for a synthesized result."""
    if payload.get("type") != "assistant":
        return ""
    content = (payload.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def usage_event(session_id: str, event: Any) -> dict[str, Any]:
    """An engine ``usage_snapshot`` as ``ca_usage_snapshot``."""
    return _drop_none(
        {
            "kind": "ca_usage_snapshot",
            "sessionId": session_id,
            "inputTokens": event.input_tokens,
            "outputTokens": event.output_tokens,
            "cacheCreationInputTokens": event.cache_creation_input_tokens,
            "cacheReadInputTokens": event.cache_read_input_tokens,
            "costUsd": event.cost_usd,
            "costSemantic": event.cost_semantic,
        }
    )


_OUTCOMES = {"blocked", "redacted", "clean", "error"}


def redaction_event(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The input guardrail's ``policy_decision`` as ``ca_redaction_decision``."""
    outcome = payload.get("outcome") if payload.get("outcome") in _OUTCOMES else "error"
    latency = payload.get("latency_ms")
    return _drop_none(
        {
            "kind": "ca_redaction_decision",
            "sessionId": session_id,
            "stage": "llm_input",
            "outcome": outcome,
            "redacted": outcome == "redacted",
            "blocked": outcome == "blocked",
            "reason": payload.get("reason") or None,
            "engine": payload.get("engine") or None,
            "policyId": payload.get("policy_id") or None,
            "latencyMs": float(latency) if isinstance(latency, (int, float)) else None,
        }
    )


_TOOL_OUTCOMES = {"allowed", "blocked", "audit_violation", "audit_clean", "error"}


def tool_decision_event(session_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """A tool gate's ``policy_decision`` as ``ca_permission_decision`` (source policy)."""
    call_id = str(payload.get("call_id") or "")
    if not call_id:
        return None
    decision = str(payload.get("decision") or "").lower()
    outcome = payload.get("outcome")
    latency = payload.get("latency_ms")
    return _drop_none(
        {
            "kind": "ca_permission_decision",
            "sessionId": session_id,
            "callId": call_id,
            "decision": "allow" if decision == "allow" else "deny",
            "reason": payload.get("reason") or None,
            "toolName": payload.get("tool_name") or None,
            "source": "policy",
            "mode": payload.get("mode") if payload.get("mode") in ("enforce", "audit") else None,
            "outcome": outcome if outcome in _TOOL_OUTCOMES else None,
            "policyId": payload.get("policy_id") or None,
            "latencyMs": float(latency) if isinstance(latency, (int, float)) else None,
        }
    )


class EventLog:
    """One session's events, numbered from 0, with replay after an id."""

    def __init__(self) -> None:
        self._events: list[tuple[int, dict[str, Any]]] = []
        self._next = 0
        self._closed = False
        self._changed = asyncio.Condition()

    @property
    def closed(self) -> bool:
        return self._closed

    async def emit(self, event: dict[str, Any]) -> None:
        async with self._changed:
            if self._closed:
                return
            self._events.append((self._next, event))
            self._next += 1
            if len(self._events) > REPLAY_LIMIT:
                del self._events[: len(self._events) - REPLAY_LIMIT]
            self._changed.notify_all()

    async def close(self) -> None:
        async with self._changed:
            self._closed = True
            self._changed.notify_all()

    async def iterate(self, *, since: int) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """Events with an id above ``since``, then live ones until the log closes."""
        cursor = since + 1
        while True:
            async with self._changed:
                pending = [e for e in self._events if e[0] >= cursor]
                if not pending:
                    if self._closed:
                        return
                    await self._changed.wait()
                    continue
            for event_id, event in pending:
                cursor = event_id + 1
                yield event_id, event


def encode_sse(event_id: int, event: dict[str, Any]) -> str:
    """One SSE frame: ``event: <kind>``, ``id: <n>``, ``data: <json>``."""
    return f"event: {event['kind']}\nid: {event_id}\ndata: {json.dumps(event)}\n\n"


__all__ = [
    "REPLAY_LIMIT",
    "EventLog",
    "assistant_text",
    "encode_sse",
    "redaction_event",
    "to_wire",
    "tool_decision_event",
    "usage_event",
]
