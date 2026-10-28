"""What runs at the start of every turn: resolve its SRS policies, hand them to
the tool gate, and check the user's message.

The turn's policy set is the org (global) policies, read fresh from
``agent_api_url`` every turn, followed by the agent's own (local) policy from
the agent document, de-duplicated. Every SRS check in the turn — the input
check here and every tool call through :class:`~computeragent.policy.srs_gate.SrsToolGate`
— is made against that one combined set. Policy ids never come from the
environment.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..policy.org_policy import OrgPolicyFetchError, fetch_org_rai_policies
from ..policy.srs_gate import SrsToolGate, TurnPolicy, build_turn_policy
from ..policy.srs_input import InputDecision, check_user_message

logger = logging.getLogger("computeragent.harness.guardrails")


@dataclass(frozen=True)
class TurnStart:
    """The outcome of a turn's guardrails."""

    #: The message to run, with SRS's masked text in place.
    message: Any
    #: Set when the message was blocked: the reply, and the engine must not run.
    blocked_reply: str | None
    decision: InputDecision
    #: ``policy_decision`` telemetry payload for the input check, when one ran.
    telemetry: dict[str, Any] | None


class SrsTurnGuard:
    """Resolves each turn's policies and checks its input."""

    def __init__(
        self,
        *,
        agent_api_url: str,
        srs_url: str,
        api_key: Callable[[], str],
        agent_name: str,
        local_policy_ids: Sequence[str],
        gate: SrsToolGate,
        client: Any | None = None,
    ) -> None:
        self._agent_api_url = agent_api_url
        self._srs_url = srs_url
        self._api_key = api_key
        self._agent_name = agent_name
        self._local_policy_ids = [i for i in local_policy_ids if i]
        self._gate = gate
        self._client = client

    async def resolve_turn(self) -> TurnPolicy:
        """This turn's combined policy set. Unresolvable when the org policies
        cannot be read: every check in the turn then blocks."""
        try:
            org = await fetch_org_rai_policies(
                self._agent_api_url, self._api_key(), client=self._client
            )
        except OrgPolicyFetchError as exc:
            logger.warning("turn policies unresolved: %s", exc)
            return TurnPolicy.unresolvable()
        turn = build_turn_policy(org, self._local_policy_ids)
        logger.info(
            "turn policies: %s (org %s)",
            ",".join(turn.policy_ids) or "none",
            ",".join(p.policy_id for p in org) or "none",
        )
        return turn

    async def start_turn(self, message: Any, *, session_id: str) -> TurnStart:
        started_at = time.monotonic()
        turn = await self.resolve_turn()
        self._gate.set_turn(turn)
        decision = await check_user_message(
            message,
            turn,
            srs_url=self._srs_url,
            api_key=self._api_key(),
            principal_id=self._agent_name,
            client=self._client,
        )
        telemetry = None
        if decision.checked or decision.blocked:
            telemetry = {
                "engine": "srs",
                "stage": "llm_input",
                "decision": "deny" if decision.blocked else "allow",
                "reason": decision.reason or "",
                "latency_ms": (time.monotonic() - started_at) * 1000.0,
                "mode": None,
                "outcome": decision.outcome,
                "policy_id": decision.policy_id,
                "policy_ids": list(turn.policy_ids),
                "entity_counts": dict(decision.entity_counts),
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
