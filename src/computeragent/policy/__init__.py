"""Policy-based tool-use authorization.

Public surface:

* :class:`~computeragent.policy.types.PolicyEngine` — Protocol implemented by
  :class:`~computeragent.policy.opa.OpaPolicyEngine` and
  :class:`~computeragent.policy.cedar.CedarPolicyEngine`.
* :class:`~computeragent.policy.types.PolicyInput` and friends
  (``PolicyPrincipal``, ``PolicyAction``, ``PolicyResource``, ``PolicyResult``,
  ``PolicyDecision``) — canonical engine-agnostic shapes.
* :class:`~computeragent.policy.authorizer.PolicyToolAuthorizer` — drop into
  ``ClaudeAgentOptions.can_use_tool`` to gate every tool call through your
  engine of choice.

Engines are imported lazily so a user who installs only ``computer-agent-py``
(no Cedar extra) doesn't pay an ``ImportError`` cost on
``from computeragent.policy import OpaPolicyEngine``.
"""

from __future__ import annotations

from .authorizer import PolicyToolAuthorizer
from .types import (
    FailMode,
    PolicyAction,
    PolicyDecision,
    PolicyEngine,
    PolicyInput,
    PolicyPrincipal,
    PolicyResource,
    PolicyResult,
)

__all__ = [
    "FailMode",
    "OpaPolicyEngine",
    "CedarPolicyEngine",
    "PolicyAction",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyInput",
    "PolicyPrincipal",
    "PolicyResource",
    "PolicyResult",
    "PolicyToolAuthorizer",
]


def __getattr__(name: str):  # type: ignore[no-untyped-def]
    """Lazy import — only raise ImportError for the engine the user asked for."""
    if name == "OpaPolicyEngine":
        from .opa import OpaPolicyEngine as _OpaPolicyEngine

        return _OpaPolicyEngine
    if name == "CedarPolicyEngine":
        from .cedar import CedarPolicyEngine as _CedarPolicyEngine

        return _CedarPolicyEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
