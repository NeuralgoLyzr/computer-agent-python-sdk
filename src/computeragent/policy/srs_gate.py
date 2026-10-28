"""The turn's policy set, and the SRS gate every tool call passes.

A port of CAS harness-server ``SrsPolicyDecider.resolveTurn`` / ``evaluate``.

:class:`TurnPolicy` is one turn's set: the org (global) policies first, in the
org's priority order, then the agent's own (local), de-duplicated. SRS stops at
the first policy that denies, so leading with the org's attributes an org-level
block to it. ``fail_open`` and ``timeout_s`` aggregate over the org policies:
one fail-closed org policy closes the turn, and the timeout is the slowest org
policy's, since one SRS call evaluates them all.

:class:`SrsToolGate` is a :class:`~computeragent.policy.types.PolicyEngine`
(wrap it in :class:`~computeragent.policy.PolicyToolAuthorizer`). For each tool
call it runs the content check, then the Cedar/OPA tool-call check, against the
whole set; the first violation decides. A check that reaches no verdict denies,
unless every org policy sets ``fail_open`` — except a 422, which is a body SRS
rejected (a contract bug, not an outage) and never fails open.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .srs_client import (
    SrsUnavailable,
    check_tool_call,
    check_tool_content,
    policy_tool_name,
)
from .types import PolicyDecision, PolicyInput, PolicyResult

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from .org_policy import OrgRaiPolicy

logger = logging.getLogger("computeragent.policy.srs")

#: Per-call SRS timeout when no org policy sets one (CAS ``SRS_TIMEOUT_MS``).
DEFAULT_SRS_TIMEOUT_S = 5.0

#: Egress-safe reason for a turn whose policies could not be resolved.
TURN_UNRESOLVED_REASON = "org RAI policy could not be loaded"


@dataclass(frozen=True)
class TurnPolicy:
    """The policies one turn is evaluated against."""

    policy_ids: tuple[str, ...]
    #: ``None`` when no org policy applies; else whether EVERY org policy is
    #: fail-open.
    fail_open: bool | None
    timeout_s: float
    #: Set when the org policies could not be read: every check denies.
    unresolved: bool = False

    @classmethod
    def unresolvable(cls) -> TurnPolicy:
        return cls(policy_ids=(), fail_open=None, timeout_s=DEFAULT_SRS_TIMEOUT_S, unresolved=True)


def build_turn_policy(org: Sequence[OrgRaiPolicy], local_ids: Sequence[str]) -> TurnPolicy:
    """Org policies first, then the agent's own, de-duplicated."""
    org_ids = [p.policy_id for p in org]
    policy_ids = tuple(dict.fromkeys([*org_ids, *(i for i in local_ids if i)]))
    if not org:
        return TurnPolicy(policy_ids=policy_ids, fail_open=None, timeout_s=DEFAULT_SRS_TIMEOUT_S)
    return TurnPolicy(
        policy_ids=policy_ids,
        fail_open=all(p.fail_open for p in org),
        timeout_s=max(p.timeout_s for p in org),
    )


class SrsToolGate:
    """Every tool call through SRS: content check, then Cedar/OPA."""

    name = "srs"

    def __init__(
        self,
        *,
        srs_url: str,
        api_key: Callable[[], str],
        agent_name: str,
        engine_name: str,
        mcp_server_names: Sequence[str] = (),
        client: Any | None = None,
    ) -> None:
        self._srs_url = srs_url
        self._api_key = api_key
        self._agent_name = agent_name
        self._engine_name = engine_name
        self._mcp_server_names = list(mcp_server_names)
        self._client = client
        self._turn: TurnPolicy | None = None

    @property
    def turn(self) -> TurnPolicy | None:
        return self._turn

    def set_turn(self, turn: TurnPolicy) -> None:
        """The policy set for the turn about to run. Called at each turn start."""
        self._turn = turn

    async def evaluate(self, input: PolicyInput) -> PolicyResult:
        turn = self._turn
        if turn is None or turn.unresolved:
            # The org's fail_open is a property of the row that could not be
            # read, so it cannot be honoured.
            return PolicyResult(
                decision=PolicyDecision.DENY,
                reason=TURN_UNRESOLVED_REASON,
                engine=self.name,
                outcome="error",
            )
        if not turn.policy_ids:
            # No org policy and no agent binding: nothing to enforce.
            return PolicyResult(decision=PolicyDecision.ALLOW, engine=self.name, outcome="allowed")

        raw_name = input.action.tool_name
        tool_args = dict(input.action.tool_input or {})
        name = policy_tool_name(
            raw_name, engine_name=self._engine_name, mcp_server_names=self._mcp_server_names
        )
        session_id = str(input.context.get("session_id") or input.resource.session_id or "")
        api_key = self._api_key()
        logger.info(
            "SRS tool check %s (raw %s) against %s", name, raw_name, ",".join(turn.policy_ids)
        )
        try:
            violation = await check_tool_content(
                self._srs_url,
                api_key=api_key,
                policy_ids=turn.policy_ids,
                tool_name=name,
                tool_args=tool_args,
                call_id=None,
                agent_name=self._agent_name,
                session_id=session_id,
                principal_id=self._agent_name,
                timeout_s=turn.timeout_s,
                client=self._client,
            ) or await check_tool_call(
                self._srs_url,
                api_key=api_key,
                policy_ids=turn.policy_ids,
                tool_name=name,
                raw_tool_name=raw_name,
                tool_args=tool_args,
                agent_name=self._agent_name,
                principal_id=self._agent_name,
                timeout_s=turn.timeout_s,
                client=self._client,
            )
        except SrsUnavailable as exc:
            return self._no_verdict(turn, exc)

        if violation is None:
            return PolicyResult(
                decision=PolicyDecision.ALLOW,
                engine=self.name,
                outcome="allowed",
                policy_id=turn.policy_ids[0],
            )
        logger.warning(
            "SRS denied %s (%s, policy %s)", name, violation.denied_by, violation.policy_id or "?"
        )
        return PolicyResult(
            decision=PolicyDecision.DENY,
            reason=violation.reason,
            engine=self.name,
            outcome="blocked",
            # The policy that acted when SRS named it, else the first evaluated.
            policy_id=violation.policy_id or turn.policy_ids[0],
        )

    async def aclose(self) -> None:
        """Nothing to release: each check opens its own client unless one was
        injected, and an injected client is the caller's to close."""
        return

    def _no_verdict(self, turn: TurnPolicy, exc: SrsUnavailable) -> PolicyResult:
        fail_open = turn.fail_open is True and exc.status != 422
        if fail_open:
            logger.warning(
                "SRS gave no verdict (%s); every org policy sets fail_open, so the tool runs "
                "UNCHECKED",
                exc,
            )
        return PolicyResult(
            decision=PolicyDecision.ALLOW if fail_open else PolicyDecision.DENY,
            reason=str(exc),
            engine=self.name,
            outcome="error",
            policy_id=turn.policy_ids[0],
        )


__all__ = [
    "DEFAULT_SRS_TIMEOUT_S",
    "TURN_UNRESOLVED_REASON",
    "SrsToolGate",
    "TurnPolicy",
    "build_turn_policy",
]
