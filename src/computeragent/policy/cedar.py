"""``CedarPolicyEngine`` — in-process Cedar evaluation via ``cedarpy``.

Cedar policies and entities are compiled / loaded at engine construction.
Each ``evaluate`` call translates :class:`PolicyInput` into Cedar's request
shape, then calls ``cedarpy.is_authorized``.

Translation rules:

* ``PolicyPrincipal(id="alice", groups=["engineer"])``
  → ``User::"alice"`` with parents ``[Group::"engineer", ...]``.
* ``PolicyAction(name="tool_use", tool_name="Read")``
  → ``Action::"toolUse"`` with attribute ``tool: "Read"``.
* ``PolicyResource(type="agent", agent_name="nordassist")``
  → ``Agent::"nordassist"``.

Optional extra: ``pip install 'computer-agent-py[cedar]'``.
"""

from __future__ import annotations

import logging
from typing import Any

try:
    import cedarpy as _cedarpy
except ImportError as _exc:  # pragma: no cover - guarded by lazy attribute
    raise ImportError(
        "CedarPolicyEngine requires the [cedar] extra. "
        "Install with: pip install 'computer-agent-py[cedar]'"
    ) from _exc

from .types import FailMode, PolicyDecision, PolicyInput, PolicyResult

logger = logging.getLogger("computeragent.policy")


class CedarPolicyEngine:
    """Local Cedar engine. No network calls.

    Pass either ``policies`` (one or more Cedar policy texts) or ``schema``
    (Cedar schema text). ``entities`` is an optional pre-loaded list using
    cedarpy's entity dict shape; per-request entities (one for principal,
    action, and resource) are merged in automatically.
    """

    name = "cedar"

    def __init__(
        self,
        *,
        policies: str | list[str],
        entities: list[dict[str, Any]] | None = None,
        schema: str | None = None,
        fail_mode: FailMode = "deny",
    ) -> None:
        self._policies = "\n".join(policies) if isinstance(policies, list) else policies
        self._schema = schema
        self._static_entities = list(entities or [])
        self._fail_mode = fail_mode

        # Fail fast on policy parse errors at startup, not at first request.
        try:
            _cedarpy.format_policies(self._policies)
        except Exception as exc:  # noqa: BLE001 - cedarpy raises various types
            raise ValueError(f"Cedar policy parse error: {exc}") from exc

    async def evaluate(self, input: PolicyInput) -> PolicyResult:
        try:
            request, entities = _build_request_and_entities(input, self._static_entities)
            result = _cedarpy.is_authorized(
                request=request,
                policies=self._policies,
                entities=entities,
                schema=self._schema,
            )
            allow = result.decision == _cedarpy.Decision.Allow
            reasons = list(getattr(result.diagnostics, "reasons", []) or [])
            errors = list(getattr(result.diagnostics, "errors", []) or [])
            reason_parts: list[str] = []
            if reasons:
                reason_parts.append(f"policies={','.join(reasons)}")
            if errors:
                reason_parts.append(f"errors={','.join(str(e) for e in errors)}")
            reason = "; ".join(reason_parts) or ("cedar allowed" if allow else "cedar denied")
            return PolicyResult(
                decision=PolicyDecision.ALLOW if allow else PolicyDecision.DENY,
                reason=reason,
                engine=self.name,
            )
        except Exception as exc:  # noqa: BLE001 - cedarpy raises bare exceptions
            return self._failure_result(exc)

    async def aclose(self) -> None:
        # Nothing to close — cedarpy is purely in-process.
        return

    # ── helpers ────────────────────────────────────────────────────────────

    def _failure_result(self, exc: BaseException) -> PolicyResult:
        if self._fail_mode == "allow":
            logger.warning("Cedar policy engine error; fail_mode=allow → allowing: %s", exc)
            return PolicyResult(
                decision=PolicyDecision.ALLOW,
                reason=f"policy engine unavailable (fail_mode=allow): {exc}",
                engine=self.name,
            )
        logger.warning("Cedar policy engine error; fail_mode=deny → denying: %s", exc)
        return PolicyResult(
            decision=PolicyDecision.DENY,
            reason=f"policy engine unavailable: {exc}",
            engine=self.name,
        )


def _build_request_and_entities(
    input: PolicyInput,
    static_entities: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Translate a PolicyInput into cedarpy's ``request`` + ``entities`` shape."""
    user_uid = {"type": "User", "id": input.principal.id or "anonymous"}
    agent_uid = {
        "type": "Agent",
        "id": input.resource.agent_name or input.resource.session_id or "anonymous",
    }

    # Build the per-request entities: principal + groups + agent.
    group_entities: list[dict[str, Any]] = [
        {"uid": {"type": "Group", "id": g}, "attrs": {}, "parents": []}
        for g in input.principal.groups
    ]
    principal_entity = {
        "uid": user_uid,
        "attrs": _coerce_attrs(input.principal.attributes),
        "parents": [{"type": "Group", "id": g} for g in input.principal.groups],
    }
    resource_entity = {
        "uid": agent_uid,
        "attrs": _coerce_attrs(
            {
                "model": input.resource.model,
                "session_id": input.resource.session_id,
                **(input.resource.attributes or {}),
            }
        ),
        "parents": [],
    }
    # Cedar's action attributes live on the action entity itself; tool_name
    # is the most useful key to expose for `when` clauses like
    # `action.tool == "Read"`.
    action_entity = {
        "uid": {"type": "Action", "id": "toolUse"},
        "attrs": _coerce_attrs(
            {
                "tool": input.action.tool_name,
                # tool_input is omitted from attrs by default — Cedar prefers
                # primitives, and tool inputs can be arbitrary dicts. Users
                # who want to write policies against tool inputs should
                # supply a context_resolver that flattens specific keys.
            }
        ),
        "parents": [],
    }
    entities = [
        *static_entities,
        principal_entity,
        resource_entity,
        action_entity,
        *group_entities,
    ]

    request = {
        "principal": f'User::"{user_uid["id"]}"',
        "action": 'Action::"toolUse"',
        "resource": f'Agent::"{agent_uid["id"]}"',
        "context": _coerce_attrs(input.context),
    }
    return request, entities


def _coerce_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """Drop None values; Cedar treats missing attribute as ``has`` = false."""
    return {k: v for k, v in attrs.items() if v is not None and v != ""}
