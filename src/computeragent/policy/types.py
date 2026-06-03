"""Canonical policy types — engine-agnostic dataclasses + the ``PolicyEngine``
Protocol that OPA and Cedar implementations satisfy.

The same :class:`PolicyInput` shape is sent to both engines so users can switch
between OPA and Cedar without changing the worker code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class PolicyPrincipal:
    """Who is requesting the action — user / session / agent identifier."""

    id: str
    groups: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "groups": list(self.groups),
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True)
class PolicyAction:
    """What is being attempted. v0.2 covers tool_use; ``name`` is extensible."""

    name: Literal["tool_use"]
    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tool_name": self.tool_name,
            "tool_input": dict(self.tool_input),
        }


@dataclass(frozen=True)
class PolicyResource:
    """What the action is being attempted *on* — typically the running agent."""

    type: str = "agent"
    agent_name: str = ""
    model: str = ""
    session_id: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "agent_name": self.agent_name,
            "model": self.model,
            "session_id": self.session_id,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True)
class PolicyInput:
    """One authorization request. Identical shape across OPA and Cedar engines."""

    principal: PolicyPrincipal
    action: PolicyAction
    resource: PolicyResource
    context: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "principal": self.principal.to_json(),
            "action": self.action.to_json(),
            "resource": self.resource.to_json(),
            "context": dict(self.context),
        }


class PolicyDecision(Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyResult:
    """Engine output — decision plus human-readable reason + optional obligations.

    Obligations are advisory in v0.2 (not enforced); kept on the result so
    sinks / audit logs can surface them.
    """

    decision: PolicyDecision
    reason: str = ""
    obligations: list[dict[str, Any]] = field(default_factory=list)
    engine: str = ""  # populated by the concrete engine ("opa" | "cedar")


class PolicyEngine(Protocol):
    """Pluggable backend for tool-use authorization.

    Implementations: :class:`~computeragent.policy.opa.OpaPolicyEngine` (remote
    HTTP) and :class:`~computeragent.policy.cedar.CedarPolicyEngine` (in-process
    via ``cedarpy``).
    """

    name: str
    """Short identifier used in telemetry events (``"opa"`` / ``"cedar"``)."""

    async def evaluate(self, input: PolicyInput) -> PolicyResult: ...

    async def aclose(self) -> None: ...


FailMode = Literal["deny", "allow"]
"""What to return when the engine itself raises (network error, parse error)."""
