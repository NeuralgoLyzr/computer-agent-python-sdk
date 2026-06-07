"""``TelemetryEvent`` — the unit that flows through the telemetry pipeline.

One event is emitted for each meaningful thing that happens during a query:
session start, each message yielded, each tool call, the final usage snapshot,
and session end. Middleware can mutate ``payload`` (the user-visible projection)
or drop the event entirely; sinks consume the event and write it somewhere.

The ``raw`` reference points back to the original upstream object so sinks
that need the verbatim shape (e.g. a debug audit log) can grab it. Sinks that
ship content to external systems should read from ``payload`` so middleware
redaction is honoured.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

logger = logging.getLogger("computeragent.telemetry")

EventKind = Literal[
    "session_started",
    "turn_started",
    "user_message",
    "assistant_message",
    "system_message",
    "tool_use",
    "tool_result",
    "usage_snapshot",
    "session_ended",
    "policy_decision",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TelemetryEvent:
    """Single unit of telemetry. Mutable: middleware modifies ``payload`` in place."""

    kind: EventKind
    session_id: str
    timestamp: datetime = field(default_factory=_utcnow)
    agent_name: str | None = None
    # Stable, caller-supplied agent identifier. Decoupled from ``agent_name``
    # (the display label) so renaming an agent doesn't re-register it. Stamped
    # by the coordinator at emit time onto every event; rides the AgentOS ingest
    # so the server can key the registry + per-agent joins on it. ``None`` →
    # the server falls back to keying on ``agent_name`` (legacy behavior).
    agent_id: str | None = None
    # Human-readable description of the agent. Mirrors `agent_name`'s
    # opt-in shape: derived from ``options.agent_description`` (set via
    # setattr by the caller) and propagated through the same event path.
    # ``None`` when the caller doesn't supply one — sinks fall back to an
    # empty string in the registry doc.
    agent_description: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    raw: Any = None

    # ──────────────────────────────────────────────────────────────────────
    # Factories — keep all the upstream-shape-to-event-shape mapping in one
    # place so message_translate-style spaghetti doesn't end up scattered.
    # ──────────────────────────────────────────────────────────────────────

    @classmethod
    def session_started(
        cls,
        *,
        session_id: str,
        agent_name: str | None,
        agent_description: str | None = None,
        options: Any,
        prompt: Any,
    ) -> TelemetryEvent:
        # Capture only metadata that a sink might want — never the full options
        # object, which can hold callables (hooks, can_use_tool) that don't
        # serialize.
        # `system_prompt` may be a string OR a preset/file object — only persist
        # the string form, since the inline-source manifest we build downstream
        # treats it as plain CLAUDE.md content.
        sp = getattr(options, "system_prompt", None)
        system_prompt = sp if isinstance(sp, str) else None
        payload: dict[str, Any] = {
            "model": getattr(options, "model", None),
            "system_prompt": system_prompt,
            "permission_mode": getattr(options, "permission_mode", None),
            "allowed_tools": list(getattr(options, "allowed_tools", []) or []),
            "disallowed_tools": list(getattr(options, "disallowed_tools", []) or []),
            "max_turns": getattr(options, "max_turns", None),
            "cwd": str(getattr(options, "cwd", "") or ""),
            "add_dirs": [str(p) for p in (getattr(options, "add_dirs", []) or [])],
            "prompt": prompt if isinstance(prompt, str) else "<streaming>",
        }
        return cls(
            kind="session_started",
            session_id=session_id,
            agent_name=agent_name,
            agent_description=agent_description,
            payload=payload,
            raw=options,
        )

    @classmethod
    def turn_started(
        cls,
        *,
        session_id: str,
        agent_name: str | None,
        agent_description: str | None = None,
        prompt: Any,
        model: str | None = None,
    ) -> TelemetryEvent:
        """Per-turn boundary. Mirrors the TS ``ca_turn_started`` — the OtelSink
        closes the prior turn's tree and opens a fresh ``invoke_agent`` root.

        Emitted on EVERY turn (the first chat and each subsequent one). Other
        sinks ignore it; only ``OtelSink`` consumes it.
        """
        return cls(
            kind="turn_started",
            session_id=session_id,
            agent_name=agent_name,
            agent_description=agent_description,
            payload={
                "prompt": prompt if isinstance(prompt, str) else "<streaming>",
                "model": model,
            },
        )

    @classmethod
    def session_ended_ok(
        cls,
        *,
        session_id: str,
        agent_name: str | None,
        result: Any,
        duration_ms: float,
        last_assistant_text: str,
    ) -> TelemetryEvent:
        payload: dict[str, Any] = {
            "is_error": bool(getattr(result, "is_error", False)),
            "subtype": getattr(result, "subtype", "success"),
            "result": getattr(result, "result", "") or last_assistant_text,
            "num_turns": int(getattr(result, "num_turns", 0) or 0),
            "total_cost_usd": float(getattr(result, "total_cost_usd", 0.0) or 0.0),
            "duration_ms": duration_ms,
            "duration_api_ms": float(getattr(result, "duration_api_ms", 0.0) or 0.0),
            "usage": _extract_usage(result),
        }
        return cls(
            kind="session_ended",
            session_id=session_id,
            agent_name=agent_name,
            payload=payload,
            raw=result,
        )

    @classmethod
    def session_ended_error(
        cls,
        *,
        session_id: str,
        agent_name: str | None,
        exc: BaseException,
        duration_ms: float,
    ) -> TelemetryEvent:
        return cls(
            kind="session_ended",
            session_id=session_id,
            agent_name=agent_name,
            payload={
                "is_error": True,
                "subtype": "error",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "duration_ms": duration_ms,
                "result": "",
                "num_turns": 0,
                "total_cost_usd": 0.0,
                "usage": {},
            },
            raw=exc,
        )

    @classmethod
    def policy_decision(
        cls,
        *,
        input: Any,
        result: Any,
        error: BaseException | None,
        latency_ms: float,
        engine_name: str,
    ) -> TelemetryEvent:
        """Emitted by ``PolicyToolAuthorizer`` after each authorization check.

        ``input`` is a :class:`computeragent.policy.PolicyInput` and ``result``
        a :class:`PolicyResult` (or ``None`` if the engine raised). Sinks pick
        this up via the standard pipeline; ``OtelSink`` annotates the active
        ``execute_tool`` span with the decision and reason.
        """
        principal = getattr(input, "principal", None)
        action = getattr(input, "action", None)
        resource = getattr(input, "resource", None)
        decision_value = getattr(getattr(result, "decision", None), "value", None)
        payload: dict[str, Any] = {
            "engine": engine_name,
            "decision": decision_value or ("error" if error else "unknown"),
            "reason": getattr(result, "reason", "") if result is not None else "",
            "latency_ms": latency_ms,
            "principal_id": getattr(principal, "id", ""),
            "principal_groups": list(getattr(principal, "groups", []) or []),
            "tool_name": getattr(action, "tool_name", ""),
            "agent_name": getattr(resource, "agent_name", ""),
            "session_id": getattr(resource, "session_id", "")
            or getattr(input, "context", {}).get("session_id", ""),
        }
        if error is not None:
            payload["error_type"] = type(error).__name__
            payload["error_message"] = str(error)
        return cls(
            kind="policy_decision",
            session_id=payload["session_id"] or "unknown",
            agent_name=payload["agent_name"] or None,
            payload=payload,
            raw=result if result is not None else error,
        )

    @classmethod
    def from_message(
        cls, msg: Any, *, session_id: str, agent_name: str | None
    ) -> list[TelemetryEvent]:
        """Map one upstream message to zero or more telemetry events.

        The kinds emitted depend on the upstream message type and any content
        blocks it carries. The translation here is deliberately tolerant: if
        upstream adds a new field we don't recognise, we just don't emit
        anything for it rather than raising.
        """
        # Lazy import — only need the types at runtime when translation runs.
        from claude_agent_sdk.types import (
            AssistantMessage,
            ResultMessage,
            SystemMessage,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        events: list[TelemetryEvent] = []

        if isinstance(msg, AssistantMessage):
            text_parts: list[str] = []
            has_tool_use = False
            for block in getattr(msg, "content", []) or []:
                if isinstance(block, ToolUseBlock):
                    has_tool_use = True
                    events.append(
                        cls(
                            kind="tool_use",
                            session_id=session_id,
                            agent_name=agent_name,
                            payload={
                                "tool_use_id": getattr(block, "id", ""),
                                "tool_name": getattr(block, "name", ""),
                                "tool_input": dict(getattr(block, "input", {}) or {}),
                            },
                            raw=block,
                        )
                    )
                else:
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        text_parts.append(text)
            events.append(
                cls(
                    kind="assistant_message",
                    session_id=session_id,
                    agent_name=agent_name,
                    payload={
                        "model": getattr(msg, "model", None),
                        "text": "".join(text_parts),
                        "block_count": len(getattr(msg, "content", []) or []),
                        # stop_reason + has_tool_use feed gen_ai.response.finish_reasons
                        # on the chat span (OtelSink._map_finish_reason).
                        "stop_reason": getattr(msg, "stop_reason", None),
                        "has_tool_use": has_tool_use,
                    },
                    raw=msg,
                )
            )
        elif isinstance(msg, UserMessage):
            tool_results: list[dict[str, Any]] = []
            text_parts = []
            for block in getattr(msg, "content", []) or []:
                if isinstance(block, ToolResultBlock):
                    tool_results.append(
                        {
                            "tool_use_id": getattr(block, "tool_use_id", ""),
                            "is_error": bool(getattr(block, "is_error", False)),
                            "content": getattr(block, "content", ""),
                        }
                    )
                else:
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        text_parts.append(text)
            if tool_results:
                for tr in tool_results:
                    events.append(
                        cls(
                            kind="tool_result",
                            session_id=session_id,
                            agent_name=agent_name,
                            payload=tr,
                            raw=msg,
                        )
                    )
            if text_parts:
                events.append(
                    cls(
                        kind="user_message",
                        session_id=session_id,
                        agent_name=agent_name,
                        payload={"text": "".join(text_parts)},
                        raw=msg,
                    )
                )
        elif isinstance(msg, SystemMessage):
            events.append(
                cls(
                    kind="system_message",
                    session_id=session_id,
                    agent_name=agent_name,
                    payload={
                        "subtype": getattr(msg, "subtype", ""),
                        "data": dict(getattr(msg, "data", {}) or {}),
                    },
                    raw=msg,
                )
            )
        elif isinstance(msg, ResultMessage):
            # ResultMessage also drives a `usage_snapshot` so token + cost
            # telemetry doesn't only appear at session_ended (sinks that
            # record metrics per session want the histogram measurement now).
            events.append(
                cls(
                    kind="usage_snapshot",
                    session_id=session_id,
                    agent_name=agent_name,
                    payload={
                        "model": _model_from_options_or_result(msg),
                        "total_cost_usd": float(getattr(msg, "total_cost_usd", 0.0) or 0.0),
                        "usage": _extract_usage(msg),
                    },
                    raw=msg,
                )
            )
        # Any remaining claude_agent_sdk type (StreamEvent, HookEventMessage,
        # RateLimitEvent, TaskStartedMessage, …) — return [] so the coordinator
        # skips synthesis. Non-SDK payloads (gitagent dicts, OpenAI messages)
        # return None to trigger _synthesise_telemetry instead.
        if type(msg).__module__.startswith("claude_agent_sdk"):
            return events  # empty list — recognised SDK type, nothing to emit
        return None  # unrecognised non-SDK payload — caller should synthesise


def _extract_usage(result_like: Any) -> dict[str, int | float | None]:
    """Pull tokens + cache fields out of a ResultMessage's nested usage shape."""
    usage = getattr(result_like, "usage", None)
    if usage is None:
        return {}
    # Pydantic-style models, dataclasses, or dicts — handle all three.
    if hasattr(usage, "model_dump"):
        try:
            return dict(usage.model_dump())
        except Exception:  # noqa: BLE001 - defensive
            logger.debug("usage.model_dump() failed", exc_info=True)
    if hasattr(usage, "__dataclass_fields__"):
        return {k: getattr(usage, k, None) for k in usage.__dataclass_fields__}
    if isinstance(usage, dict):
        return dict(usage)
    # Last resort — pick up the conventional Anthropic fields if they exist.
    return {
        k: getattr(usage, k, None)
        for k in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
        if hasattr(usage, k)
    }


def _model_from_options_or_result(result: Any) -> str | None:
    return getattr(result, "model", None)


def new_session_id() -> str:
    """Generate a placeholder session id used before the upstream returns one."""
    return f"ca_{uuid.uuid4().hex}"
