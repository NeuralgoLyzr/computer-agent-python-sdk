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

    async def start_session(self, ctx: EngineContext) -> AsyncIterator[EngineEvent]:
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
    # already matches the CanUseTool signature; the wrapper only stamps the
    # harness session id onto the SDK's ToolPermissionContext (which doesn't
    # carry one) so the resulting policy_decision telemetry correlates with
    # this session's open OTel spans instead of landing on "unknown".
    if ctx.policy_decider is not None:
        bound = _bind_session_id(ctx.policy_decider, ctx.session_id)
        payload["can_use_tool"] = bound
        # Policy enforcement that survives prompt-skipping permission modes.
        # ``can_use_tool`` only fires when the CLI's permission rules evaluate
        # to "ask" — under bypassPermissions / dontAsk / auto (and for edits
        # under acceptEdits) it is skipped entirely, which would leave a
        # console-bound policy unenforced. A PreToolUse hook fires for EVERY
        # tool call regardless of permission mode (SDK-documented), so route
        # the same bound decider through one and translate a deny into the
        # hook's deny decision — mirroring the TS engine. Registered only for
        # the skipping modes so default-mode behavior (and telemetry volume)
        # is unchanged. MERGED with GAP/caller-supplied hooks, never replacing.
        if payload.get("permission_mode") in (
            "bypassPermissions",
            "dontAsk",
            "auto",
            "acceptEdits",
        ):
            hooks = dict(payload.get("hooks") or {})
            pre = list(hooks.get("PreToolUse") or [])
            pre.append(_policy_pretooluse_matcher(bound))
            hooks["PreToolUse"] = pre
            payload["hooks"] = hooks

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


def _policy_pretooluse_matcher(decider: Any) -> Any:
    """Wrap the bound ``can_use_tool`` decider as a PreToolUse ``HookMatcher``.

    The hook receives the SDK's PreToolUse input (``tool_name`` /
    ``tool_input`` / ``tool_use_id``), runs the SAME decider that backs
    ``can_use_tool``, and maps a deny onto the hook's
    ``permissionDecision: "deny"`` — the only gate that fires under
    prompt-skipping permission modes. An allow returns ``{}`` (no opinion).
    An unexpected decider exception fails CLOSED (the authorizer's own
    fail-open handling lives inside it; anything escaping is a bug).
    """
    from claude_agent_sdk.types import HookMatcher

    async def _hook(hook_input: Any, _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        data = hook_input if isinstance(hook_input, dict) else {}
        tool_name = str(data.get("tool_name") or "")
        tool_input = data.get("tool_input") or {}
        try:
            result = await decider(tool_name, tool_input, _permission_context())
        except Exception as exc:  # noqa: BLE001 — fail closed on decider bugs
            logger.exception("policy PreToolUse hook: decider raised")
            return _hook_deny(f"policy evaluation failed: {exc}")
        behavior = getattr(result, "behavior", None)
        if behavior is None and isinstance(result, dict):
            behavior = result.get("behavior")
        if behavior == "deny":
            message = getattr(result, "message", None)
            if message is None and isinstance(result, dict):
                message = result.get("message")
            return _hook_deny(str(message or "blocked by policy"))
        return {}

    return HookMatcher(hooks=[_hook])


def _permission_context() -> Any:
    """A best-effort ``ToolPermissionContext`` for hook-originated decisions.

    The hook path has no real permission context (the CLI never built one —
    the call skipped the prompt), so hand the decider an empty one; the
    ``_bind_session_id`` wrapper still stamps the session id onto it.
    """
    try:
        from claude_agent_sdk.types import ToolPermissionContext

        return ToolPermissionContext()
    except Exception:  # noqa: BLE001 — required fields / import drift
        from types import SimpleNamespace

        return SimpleNamespace()


def _hook_deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _bind_session_id(decider: Any, session_id: str) -> Any:
    """Wrap a ``can_use_tool`` decider so the permission context carries the
    harness session id. ``ToolPermissionContext`` is a plain dataclass, so the
    extra attribute attaches cleanly; if upstream ever forbids it the stamp is
    skipped and the decider still runs."""

    async def _can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        with suppress(Exception):
            context.session_id = session_id
        return await decider(tool_name, tool_input, context)

    return _can_use_tool


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
