"""``CedarPolicyEngine`` — real (in-process) Cedar evaluation via ``cedarpy``."""

from __future__ import annotations

import pytest

cedarpy = pytest.importorskip("cedarpy")

from computeragent.policy import (  # noqa: E402
    PolicyAction,
    PolicyDecision,
    PolicyInput,
    PolicyPrincipal,
    PolicyResource,
)
from computeragent.policy.cedar import CedarPolicyEngine  # noqa: E402

ENGINEER_ALLOW_POLICY = """
permit (
    principal,
    action == Action::"toolUse",
    resource is Agent
) when {
    action.tool == "Read" ||
    action.tool == "Glob" ||
    action.tool == "Grep"
};
"""

GROUP_GATED_POLICY = """
permit (
    principal in Group::"engineer",
    action == Action::"toolUse",
    resource is Agent
) when {
    action.tool like "mcp__nordassist-tools__*"
};
"""


def _input(*, tool: str = "Read", groups: list[str] | None = None) -> PolicyInput:
    return PolicyInput(
        principal=PolicyPrincipal(id="alice", groups=list(groups or [])),
        action=PolicyAction(name="tool_use", tool_name=tool),
        resource=PolicyResource(agent_name="nord", model="claude-sonnet-4-5"),
        context={"env": "prod"},
    )


@pytest.mark.asyncio
async def test_allow_when_policy_matches():
    engine = CedarPolicyEngine(policies=ENGINEER_ALLOW_POLICY)
    result = await engine.evaluate(_input(tool="Read"))
    assert result.decision == PolicyDecision.ALLOW
    assert result.engine == "cedar"


@pytest.mark.asyncio
async def test_deny_when_no_policy_matches():
    engine = CedarPolicyEngine(policies=ENGINEER_ALLOW_POLICY)
    result = await engine.evaluate(_input(tool="Bash"))
    assert result.decision == PolicyDecision.DENY


@pytest.mark.asyncio
async def test_group_membership_drives_decision():
    engine = CedarPolicyEngine(policies=GROUP_GATED_POLICY)
    # In group → allow
    allow = await engine.evaluate(
        _input(tool="mcp__nordassist-tools__page_oncall", groups=["engineer"])
    )
    assert allow.decision == PolicyDecision.ALLOW
    # Not in group → deny
    deny = await engine.evaluate(
        _input(tool="mcp__nordassist-tools__page_oncall", groups=["intern"])
    )
    assert deny.decision == PolicyDecision.DENY


@pytest.mark.asyncio
async def test_multiple_policies_combined():
    engine = CedarPolicyEngine(policies=[ENGINEER_ALLOW_POLICY, GROUP_GATED_POLICY])
    # Plain Read is allowed regardless of group.
    r1 = await engine.evaluate(_input(tool="Read"))
    # MCP tool needs the engineer group.
    r2 = await engine.evaluate(
        _input(tool="mcp__nordassist-tools__page_oncall", groups=["engineer"])
    )
    assert r1.decision == PolicyDecision.ALLOW
    assert r2.decision == PolicyDecision.ALLOW


def test_construct_with_bad_policy_raises():
    with pytest.raises(ValueError, match="Cedar policy parse error"):
        CedarPolicyEngine(policies="permit (this is not Cedar)")


@pytest.mark.asyncio
async def test_no_policies_default_denies():
    # An empty policy set is valid Cedar and means "no permits" → deny.
    engine = CedarPolicyEngine(policies="")
    result = await engine.evaluate(_input(tool="Read"))
    assert result.decision == PolicyDecision.DENY
