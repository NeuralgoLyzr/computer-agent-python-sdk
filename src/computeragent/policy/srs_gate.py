"""The guardrails gate every tool call passes.

:class:`SrsToolGate` is a :class:`~computeragent.policy.types.PolicyEngine`
(wrap it in :class:`~computeragent.policy.PolicyToolAuthorizer`). Each tool
call is sent to ``POST {guardrails_url}/v1/check`` as one ``tool-input`` check
carrying the engine's own tool-use id; the agent gateway applies the org's and
the agent's policies and owns fail-open. A call with no id, or a check that
reaches no verdict, is denied.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .guardrails_client import (
    GUARDRAILS_TIMEOUT_S,
    GuardrailsUnavailable,
    check_tool_call,
    policy_tool_name,
)
from .types import PolicyDecision, PolicyInput, PolicyResult

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = logging.getLogger("computeragent.policy.guardrails")

#: Reason for a tool call that reached the gate without the engine's id.
NO_CALL_ID_REASON = "tool call has no id"


class SrsToolGate:
    """Every tool call through the guardrails check."""

    name = "guardrails"

    def __init__(
        self,
        *,
        guardrails_url: str,
        api_key: Callable[[], str],
        engine_name: str,
        mcp_server_names: Sequence[str] = (),
        timeout_s: float = GUARDRAILS_TIMEOUT_S,
        client: Any | None = None,
    ) -> None:
        self._guardrails_url = guardrails_url
        self._api_key = api_key
        self._engine_name = engine_name
        self._mcp_server_names = list(mcp_server_names)
        self._timeout_s = timeout_s
        self._client = client

    async def evaluate(self, input: PolicyInput) -> PolicyResult:
        raw_name = input.action.tool_name
        name = policy_tool_name(
            raw_name, engine_name=self._engine_name, mcp_server_names=self._mcp_server_names
        )
        call_id = str(input.context.get("call_id") or "")
        if not call_id:
            # Fail closed: the gateway identifies a tool call by the engine's id.
            logger.warning("tool call %s reached the guardrails gate with no id; denying", name)
            return PolicyResult(
                decision=PolicyDecision.DENY,
                reason=NO_CALL_ID_REASON,
                engine=self.name,
                outcome="error",
            )
        try:
            violation = await check_tool_call(
                self._guardrails_url,
                api_key=self._api_key(),
                call_id=call_id,
                tool_name=name,
                tool_args=dict(input.action.tool_input or {}),
                timeout_s=self._timeout_s,
                client=self._client,
            )
        except GuardrailsUnavailable as exc:
            return self._no_verdict(exc)

        if violation is None:
            return PolicyResult(decision=PolicyDecision.ALLOW, engine=self.name, outcome="allowed")
        logger.warning(
            "guardrails denied %s (%s, policy %s)",
            name,
            violation.denied_by,
            violation.policy_id or "?",
        )
        return PolicyResult(
            decision=PolicyDecision.DENY,
            reason=violation.reason,
            engine=self.name,
            outcome="blocked",
            policy_id=violation.policy_id,
        )

    async def aclose(self) -> None:
        """Nothing to release: each check opens its own client unless one was
        injected, and an injected client is the caller's to close."""
        return

    def _no_verdict(self, exc: GuardrailsUnavailable) -> PolicyResult:
        """Deny: the gateway decides fail-open, and a 5xx means it chose not to;
        a 400 is a body the SDK got wrong."""
        return PolicyResult(
            decision=PolicyDecision.DENY,
            reason=str(exc),
            engine=self.name,
            outcome="error",
        )


__all__ = ["NO_CALL_ID_REASON", "SrsToolGate"]
