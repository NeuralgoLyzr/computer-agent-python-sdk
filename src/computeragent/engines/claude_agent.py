"""``ClaudeAgentEngine`` — adapter that runs the underlying
``claude_agent_sdk.query()`` loop inside the harness coordinator.

Calls ``claude_agent_sdk.query`` **directly** rather than going through
``computeragent._proxy.query``. The proxy emits its own telemetry events
to the same pipeline the coordinator uses — composing them would double-fire
every event. Drop-in 0.1.x callers still get the proxy path; harness-mode
0.2.0 callers go through this engine and rely on the coordinator's
telemetry hot path.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import claude_agent_sdk

from ..protocol_types import (
    EngineCapabilities,
    EngineContext,
    EngineEvent,
)

logger = logging.getLogger("computeragent.engines.claude_agent")

_CAPABILITIES = EngineCapabilities(
    streaming_input=True,
    partial_messages=True,
    permission_callback=True,
    sessions=True,
    budget=True,
)


class ClaudeAgentEngine:
    name = "claude-agent-sdk"
    capabilities = _CAPABILITIES

    async def start_session(
        self, ctx: EngineContext
    ) -> AsyncIterator[EngineEvent]:
        options = _build_options(ctx)

        # claude-agent-sdk wants prompts as an AsyncIterable of
        # ``{"type": "user", "message": {...}}`` dicts when ``can_use_tool``
        # is set. The harness supplies normalised user messages in
        # ``{"role": "user", "content": ...}`` shape via the queue; wrap
        # them into the SDK's expected envelope here.
        async def _wrapped_prompts():
            async for entry in ctx.user_message_queue:
                if ctx.cancel_event.is_set():
                    return
                yield {"type": "user", "message": entry}

        upstream_kwargs: dict[str, Any] = {
            "prompt": _wrapped_prompts(),
            "options": options,
        }
        try:
            async for msg in claude_agent_sdk.query(**upstream_kwargs):
                if ctx.cancel_event.is_set():
                    with suppress(Exception):
                        await _maybe_interrupt(msg)
                    return
                yield EngineEvent(kind="sdk_message", payload=msg)
                if _is_result_message(msg):
                    yield EngineEvent(
                        kind="usage_snapshot",
                        cost_semantic="cumulative",
                        cost_usd=float(getattr(msg, "total_cost_usd", 0.0) or 0.0),
                        **_usage_fields(msg),
                    )
        except Exception:
            logger.exception("ClaudeAgentEngine.start_session raised")
            raise


def _build_options(ctx: EngineContext) -> Any:
    """Construct a ``ClaudeAgentOptions`` from the harness context."""
    from claude_agent_sdk import ClaudeAgentOptions

    # The manifest carries raw kwargs the caller passed via ``options=...``.
    # Filter to only those keys ``ClaudeAgentOptions`` accepts so a stray
    # field (e.g. ``model`` typo) raises clearly rather than silently
    # being attached as an attribute.
    accepted = set(ClaudeAgentOptions.__dataclass_fields__.keys())
    payload: dict[str, Any] = {}
    for key, value in (ctx.manifest.options or {}).items():
        if key in accepted:
            payload[key] = value

    payload.setdefault("cwd", str(ctx.workspace_path))
    if ctx.envs:
        env_existing = dict(payload.get("env") or {})
        env_existing.update(ctx.envs)
        payload["env"] = env_existing
    # Always stream partials so the harness sees mid-turn assistant text.
    payload.setdefault("include_partial_messages", True)
    # Wire policy decider into the can_use_tool slot. ``PolicyToolAuthorizer``
    # already matches the CanUseTool signature, so no glue.
    if ctx.policy_decider is not None:
        payload["can_use_tool"] = ctx.policy_decider

    options = ClaudeAgentOptions(**payload)
    # Surface harness-supplied agent identity to the proxy's
    # ``_derive_agent_*`` helpers in case telemetry sinks (or downstream
    # exporters) introspect these. Setattr instead of ``ClaudeAgentOptions(**...)``
    # field — the SDK's class doesn't formally declare them but does
    # accept arbitrary attributes via ``__setattr__``.
    with suppress(Exception):
        if ctx.manifest.name:
            options.agent_name = ctx.manifest.name  # type: ignore[attr-defined]
    return options


def _is_result_message(msg: Any) -> bool:
    try:
        from claude_agent_sdk.types import ResultMessage  # type: ignore

        return isinstance(msg, ResultMessage)
    except ImportError:
        return False


def _usage_fields(msg: Any) -> dict[str, int | None]:
    usage = getattr(msg, "usage", None)
    if usage is None:
        return {}
    fields: dict[str, int | None] = {}
    for src_key, dest_key in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cache_creation_input_tokens", "cache_creation_input_tokens"),
        ("cache_read_input_tokens", "cache_read_input_tokens"),
    ):
        value = getattr(usage, src_key, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(src_key)
        if value is not None:
            fields[dest_key] = int(value)
    return fields


async def _maybe_interrupt(msg: Any) -> None:
    # Best-effort cancellation. ``claude_agent_sdk.query`` doesn't expose
    # an interrupt() hook on the iterator itself; the harness setting
    # ``cancel_event`` simply causes us to bail out of the loop.
    return None


__all__ = ["ClaudeAgentEngine"]
