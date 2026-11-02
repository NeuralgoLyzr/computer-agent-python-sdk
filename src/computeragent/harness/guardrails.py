"""What runs at the start of every turn: check the user's message.

The message goes to the guardrails check through the egress proxy; the agent
gateway applies the org's and the agent's policies. The SDK reads no policies
and sends no policy ids. Tool calls are checked separately, by
:class:`~computeragent.policy.srs_gate.SrsToolGate`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..policy.guardrails_client import GUARDRAILS_TIMEOUT_S
from ..policy.srs_input import InputDecision, check_user_message

logger = logging.getLogger("computeragent.harness.guardrails")


@dataclass(frozen=True)
class TurnStart:
    """The outcome of a turn's guardrails."""

    #: The message to run, with the gateway's masked text in place.
    message: Any
    #: Set when the message was blocked: the reply, and the engine must not run.
    blocked_reply: str | None
    decision: InputDecision
    #: ``policy_decision`` telemetry payload for the input check, when one ran.
    telemetry: dict[str, Any] | None


class SrsTurnGuard:
    """Checks each turn's input."""

    def __init__(
        self,
        *,
        guardrails_url: str,
        api_key: Callable[[], str],
        agent_name: str,
        timeout_s: float = GUARDRAILS_TIMEOUT_S,
        client: Any | None = None,
    ) -> None:
        self._guardrails_url = guardrails_url
        self._api_key = api_key
        self._agent_name = agent_name
        self._timeout_s = timeout_s
        self._client = client

    async def start_turn(self, message: Any, *, session_id: str) -> TurnStart:
        started_at = time.monotonic()
        decision = await check_user_message(
            message,
            guardrails_url=self._guardrails_url,
            api_key=self._api_key(),
            timeout_s=self._timeout_s,
            client=self._client,
        )
        telemetry: dict[str, Any] | None = None
        if decision.checked or decision.blocked:
            telemetry = {
                "engine": "guardrails",
                "stage": "llm_input",
                "decision": "deny" if decision.blocked else "allow",
                "reason": decision.reason or "",
                "latency_ms": (time.monotonic() - started_at) * 1000.0,
                "mode": None,
                "outcome": decision.outcome,
                "policy_id": decision.policy_id,
                "principal_id": self._agent_name,
                "principal_groups": [],
                "tool_name": "llm_input",
                "call_id": "",
                "agent_name": self._agent_name,
                "session_id": session_id,
            }
        return TurnStart(
            message=decision.message,
            blocked_reply=decision.reply if decision.blocked else None,
            decision=decision,
            telemetry=telemetry,
        )


__all__ = ["SrsTurnGuard", "TurnStart"]
