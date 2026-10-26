"""``PolicyToolAuthorizer`` — adapter from :class:`PolicyEngine` to
``claude_agent_sdk.types.CanUseTool``.

Drops straight onto ``ClaudeAgentOptions.can_use_tool``. On each tool the
agent wants to execute, the authorizer:

1. Builds a :class:`PolicyInput` using the user-supplied resolvers + the
   ``ToolPermissionContext`` upstream gives us.
2. Calls ``await engine.evaluate(input)``.
3. Returns ``PermissionResultAllow`` or ``PermissionResultDeny``.
4. Emits a ``policy_decision`` telemetry event so the OTel sink can annotate
   the active ``execute_tool`` span.

If the engine raises, the result depends on the engine's ``fail_mode`` (the
engine controls fail-closed-vs-open, not the authorizer).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
)

from ..telemetry import TelemetryEvent, get_pipeline
from .types import PolicyDecision, PolicyEngine, PolicyInput, PolicyPrincipal, PolicyResource

if TYPE_CHECKING:
    from claude_agent_sdk.types import ToolPermissionContext

logger = logging.getLogger("computeragent.policy")

PrincipalResolver = Callable[["ToolPermissionContext"], PolicyPrincipal]
ResourceResolver = Callable[["ToolPermissionContext"], PolicyResource]
ContextResolver = Callable[["ToolPermissionContext"], dict[str, Any]]
# Async variants are also acceptable — we await whatever the resolver returns
# in case the user wants to fetch metadata from a database or env.
PrincipalResolverAsync = Callable[["ToolPermissionContext"], Awaitable[PolicyPrincipal]]
ResourceResolverAsync = Callable[["ToolPermissionContext"], Awaitable[PolicyResource]]
ContextResolverAsync = Callable[["ToolPermissionContext"], Awaitable[dict[str, Any]]]


class PolicyToolAuthorizer:
    """``can_use_tool`` callable that delegates to a :class:`PolicyEngine`."""

    def __init__(
        self,
        *,
        engine: PolicyEngine,
        principal_resolver: PrincipalResolver | PrincipalResolverAsync,
        resource_resolver: ResourceResolver | ResourceResolverAsync | None = None,
        context_resolver: ContextResolver | ContextResolverAsync | None = None,
        on_deny: Literal["deny", "interrupt"] = "deny",
    ) -> None:
        self._engine = engine
        self._principal_resolver = principal_resolver
        self._resource_resolver = resource_resolver
        self._context_resolver = context_resolver
        self._on_deny = on_deny

    async def __call__(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        started_at = time.monotonic()
        # Correlation ids for the telemetry event. The upstream context is
        # either a ToolPermissionContext (claude-agent-sdk — carries
        # ``tool_use_id``; ``session_id`` is stamped on by the harness engine)
        # or the coordinator's plain dict (``{"session_id", "call_id"}``).
        # Without these the policy_decision event lands on session "unknown"
        # and the OTel sink can't annotate the live execute_tool span.
        session_id = str(_ctx_value(context, "session_id") or "")
        call_id = str(_ctx_value(context, "tool_use_id", "call_id") or "")
        input_obj = await self._build_input(tool_name, tool_input, context)
        try:
            result = await self._engine.evaluate(input_obj)
        except Exception as exc:  # noqa: BLE001 - engines are user code; never break the agent
            # The engine's own fail_mode would normally swallow this, but if
            # the engine itself misbehaves (unhandled), default to deny.
            logger.warning(
                "policy engine %r raised unexpectedly; denying",
                type(self._engine).__name__,
                exc_info=True,
            )
            permission = PermissionResultDeny(
                message=f"policy engine error: {exc}",
                interrupt=self._on_deny == "interrupt",
            )
            await self._emit_telemetry(
                input_obj, None, exc, started_at, session_id=session_id, call_id=call_id
            )
            return permission

        await self._emit_telemetry(
            input_obj, result, None, started_at, session_id=session_id, call_id=call_id
        )

        if result.decision == PolicyDecision.ALLOW:
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(
            message=result.reason or "denied by policy",
            interrupt=self._on_deny == "interrupt",
        )

    # ── helpers ────────────────────────────────────────────────────────────

    async def _build_input(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PolicyInput:
        from .types import PolicyAction  # local import to keep module surface tidy

        principal = await _maybe_await(self._principal_resolver(context))
        resource: PolicyResource
        if self._resource_resolver is not None:
            resource = await _maybe_await(self._resource_resolver(context))
        else:
            resource = PolicyResource(
                type="agent",
                session_id=str(_ctx_value(context, "session_id") or ""),
            )
        ctx: dict[str, Any]
        if self._context_resolver is not None:
            ctx = dict(await _maybe_await(self._context_resolver(context)))
        else:
            ctx = {"session_id": str(_ctx_value(context, "session_id") or "")}
        # The engine's own tool-use id: the guardrails check identifies the
        # call by it. A context resolver that set one keeps it.
        ctx.setdefault("call_id", str(_ctx_value(context, "tool_use_id", "call_id") or ""))

        return PolicyInput(
            principal=principal,
            action=PolicyAction(
                name="tool_use",
                tool_name=tool_name,
                tool_input=dict(tool_input or {}),
            ),
            resource=resource,
            context=ctx,
        )

    async def _emit_telemetry(
        self,
        input_obj: PolicyInput,
        result: Any,
        error: BaseException | None,
        started_at: float,
        *,
        session_id: str = "",
        call_id: str = "",
    ) -> None:
        latency_ms = (time.monotonic() - started_at) * 1000.0
        try:
            event = TelemetryEvent.policy_decision(
                input=input_obj,
                result=result,
                error=error,
                latency_ms=latency_ms,
                engine_name=getattr(self._engine, "name", type(self._engine).__name__),
                session_id=session_id,
                call_id=call_id,
            )
            await get_pipeline().emit(event)
        except Exception:  # noqa: BLE001
            logger.debug("policy_decision telemetry emit failed", exc_info=True)


async def _maybe_await(value: Any) -> Any:
    """Allow resolvers to be either sync (return a value) or async (return a coroutine)."""
    if hasattr(value, "__await__"):
        return await value
    return value


def _ctx_value(context: Any, *names: str) -> Any:
    """Read a correlation field off whatever shape the upstream context has —
    attribute access for ToolPermissionContext-style objects, key access for
    the coordinator's plain-dict context. First non-empty wins."""
    for name in names:
        value = getattr(context, name, None)
        if value is None and isinstance(context, dict):
            value = context.get(name)
        if value:
            return value
    return None
