"""Run several policy deciders over one tool call — deny if ANY of them denies.

Exists because the decider slots used to be a first-match-wins chain, so a
session that had both a bound policy AND a worker ``scope.write`` silently got
the policy only: its scope-guard was constructed-or-not by a condition that a
bound policy had already consumed. That was survivable while only orchestrators
carried policies; it stops being survivable once a worker inherits its
orchestrator's policy, because a worker is exactly the session that has both.

── Evaluation order ────────────────────────────────────────────────────────
Members run IN ORDER and SEQUENTIALLY — deliberately not ``asyncio.gather``.
Callers pass the cheapest first (the local scope-guard before a network
guardrail), so an out-of-scope write is denied without paying a round-trip on
every worker write.

── Two consequences worth knowing ──────────────────────────────────────────
1. A local deny SHORT-CIRCUITS, so the remote guardrail never sees that call and
   it will not appear in its audit log. The SDK's own ``policy_decision``
   telemetry still records it, so the trace is complete even though the
   guardrail service's view is not.
2. The scope-guard always enforces, so under a composite "the guardrail is in
   audit mode" no longer implies "nothing is ever blocked". That is intended
   (scope is a separate mechanism from the RAI policy), but it surprises anyone
   reading the mode off the decision alone.
"""

from __future__ import annotations

from typing import Any


def _behavior_of(result: Any) -> str | None:
    behavior = getattr(result, "behavior", None)
    if behavior is None and isinstance(result, dict):
        behavior = result.get("behavior")
    return behavior if isinstance(behavior, str) else None


class CompositePolicyDecider:
    """Deny-wins composition of several ``can_use_tool``-shaped deciders."""

    def __init__(self, members: tuple[Any, ...]) -> None:
        self.members = tuple(members)

    async def __call__(self, tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        last: Any = None
        for member in self.members:
            result = await member(tool_name, tool_input, context)
            # First denier wins, returned VERBATIM. Preserving the whole
            # decision — its message and interrupt flag — is not cosmetic: the
            # engine's hook reads `message` to tell the model why, and rewriting
            # it here would replace a scope explanation with a generic one.
            if _behavior_of(result) == "deny":
                return result
            last = result
        if last is not None:
            return last
        # No members at all. The composite factory never builds an empty one
        # (it collapses 0 → None and 1 → the member itself), but a decider that
        # silently allowed everything would be the worst possible default here.
        from claude_agent_sdk.types import PermissionResultDeny

        return PermissionResultDeny(message="no policy decider configured", interrupt=False)


def compose_deciders(members: tuple[Any, ...]) -> Any | None:
    """Collapse 0 → ``None`` and 1 → the member itself; otherwise compose."""
    present = tuple(m for m in members if m is not None)
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    return CompositePolicyDecider(present)


__all__ = ["CompositePolicyDecider", "compose_deciders"]
