"""The turn's policy set and the SRS gate on every tool call.

Ported from CAS harness-server ``SrsPolicyDecider.resolveTurn`` / ``evaluate``
and the matching cases in ``srs-policy-decider.test.ts``.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from computeragent.policy import PolicyToolAuthorizer
from computeragent.policy.org_policy import OrgRaiPolicy
from computeragent.policy.srs_gate import (
    TURN_UNRESOLVED_REASON,
    SrsToolGate,
    TurnPolicy,
    build_turn_policy,
)
from computeragent.policy.types import (
    PolicyAction,
    PolicyDecision,
    PolicyInput,
    PolicyPrincipal,
    PolicyResource,
)

SRS = "http://egress.internal/srs"
INFERENCE = f"{SRS}/v1/rai/inference"
TOOL_CALL = f"{SRS}/v1/guardrails/evaluate-tool-call"
KEY = "sbx_key"


def org(policy_id, *, fail_open=False, timeout_s=100.0):
    return OrgRaiPolicy(
        policy_id=policy_id, fail_open=fail_open, timeout_s=timeout_s, dangling=False
    )


# ── the turn's policy set ────────────────────────────────────────────────────


def test_org_policies_come_first_then_the_agents_own():
    turn = build_turn_policy([org("o1"), org("o2")], ["local"])
    assert turn.policy_ids == ("o1", "o2", "local")


def test_a_local_policy_equal_to_an_org_policy_is_sent_once():
    turn = build_turn_policy([org("shared"), org("o2")], ["shared"])
    assert turn.policy_ids == ("shared", "o2")


def test_without_org_policies_only_the_agents_own_and_the_default_timeout():
    turn = build_turn_policy([], ["local"])
    assert turn == TurnPolicy(policy_ids=("local",), fail_open=None, timeout_s=5.0)


def test_one_fail_closed_org_policy_closes_the_turn():
    turn = build_turn_policy([org("o1", fail_open=True), org("o2", fail_open=False)], [])
    assert turn.fail_open is False


def test_every_org_policy_fail_open_opens_the_turn():
    turn = build_turn_policy([org("o1", fail_open=True), org("o2", fail_open=True)], [])
    assert turn.fail_open is True


def test_the_timeout_is_the_slowest_org_policys():
    turn = build_turn_policy([org("o1", timeout_s=30), org("o2", timeout_s=120)], [])
    assert turn.timeout_s == 120


def test_no_policies_at_all():
    assert build_turn_policy([], []).policy_ids == ()
    assert build_turn_policy([], [""]).policy_ids == ()


# ── the gate ─────────────────────────────────────────────────────────────────


def gate(turn=None, *, engine="claude-agent-sdk", mcp=()):
    g = SrsToolGate(
        srs_url=SRS,
        api_key=lambda: KEY,
        agent_name="my-agent",
        engine_name=engine,
        mcp_server_names=mcp,
    )
    if turn is not None:
        g.set_turn(turn)
    return g


def tool(name="Bash", args=None, session="sess-1"):
    return PolicyInput(
        principal=PolicyPrincipal(id="my-agent"),
        action=PolicyAction(
            name="tool_use",
            tool_name=name,
            tool_input={"command": "rm -rf /tmp/x"} if args is None else args,
        ),
        resource=PolicyResource(type="agent", agent_name="my-agent", session_id=session),
        context={"session_id": session},
    )


TURN = TurnPolicy(policy_ids=("org1", "local1"), fail_open=None, timeout_s=5.0)


def mock_srs(content=None, tool_call=None):
    content_route = respx.post(INFERENCE).mock(
        return_value=content or httpx.Response(200, json={"blocked": False})
    )
    tool_route = respx.post(TOOL_CALL).mock(
        return_value=tool_call or httpx.Response(200, json={"allowed": True})
    )
    return content_route, tool_route


@respx.mock
async def test_both_checks_pass():
    content, tool_call = mock_srs()

    result = await gate(TURN).evaluate(tool())

    assert result.decision == PolicyDecision.ALLOW
    assert result.outcome == "allowed"
    assert content.called and tool_call.called


@respx.mock
async def test_both_checks_carry_the_whole_combined_list_and_the_key():
    content, tool_call = mock_srs()

    await gate(TURN).evaluate(tool())

    for route in (content, tool_call):
        assert json.loads(route.calls.last.request.content)["policy_ids"] == ["org1", "local1"]
        assert route.calls.last.request.headers["x-api-key"] == KEY


@respx.mock
async def test_the_tool_is_presented_under_its_policy_name():
    content, tool_call = mock_srs()

    await gate(TURN).evaluate(tool("Bash"))

    sent = json.loads(tool_call.calls.last.request.content)
    assert sent["tool_name"] == "claude-agent-sdk__Bash"
    assert sent["raw_tool_name"] == "Bash"
    assert sent["principal_id"] == "my-agent"
    assert sent["bundle_name"] == "my-agent"
    assert json.loads(content.calls.last.request.content)["session_id"] == "sess-1"


@respx.mock
async def test_a_gitagent_mcp_tool_gets_the_canonical_name():
    _, tool_call = mock_srs()

    await gate(TURN, engine="gitagent", mcp=["jira"]).evaluate(tool("jira__search"))

    assert json.loads(tool_call.calls.last.request.content)["tool_name"] == "mcp__jira__search"


@respx.mock
async def test_a_content_block_denies_and_skips_the_tool_call_check():
    _, tool_call = mock_srs(
        content=httpx.Response(
            200, json={"blocked": True, "block_reason": "toxic", "blocked_by_policy_id": "local1"}
        )
    )

    result = await gate(TURN).evaluate(tool())

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "toxic"
    assert result.policy_id == "local1"
    assert result.outcome == "blocked"
    assert not tool_call.called


@respx.mock
async def test_a_cedar_deny_names_the_policy_that_acted():
    mock_srs(
        tool_call=httpx.Response(
            200,
            json={
                "allowed": False,
                "denied_by": "cedar",
                "reason": "rm is forbidden",
                "denied_by_policy_id": "org1",
            },
        )
    )

    result = await gate(TURN).evaluate(tool())

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "rm is forbidden"
    assert result.policy_id == "org1"


@respx.mock
async def test_no_string_arguments_skips_only_the_content_check():
    content, tool_call = mock_srs()

    result = await gate(TURN).evaluate(tool(args={"count": 3}))

    assert result.decision == PolicyDecision.ALLOW
    assert not content.called
    assert tool_call.called


@respx.mock
async def test_no_policies_allows_without_asking_srs():
    content, tool_call = mock_srs()

    result = await gate(TurnPolicy(policy_ids=(), fail_open=None, timeout_s=5)).evaluate(tool())

    assert result.decision == PolicyDecision.ALLOW
    assert not content.called and not tool_call.called


@pytest.mark.parametrize("turn", [None, TurnPolicy.unresolvable()])
@respx.mock
async def test_an_unresolved_turn_denies_without_asking_srs(turn):
    content, tool_call = mock_srs()

    result = await gate(turn).evaluate(tool())

    assert result.decision == PolicyDecision.DENY
    assert result.reason == TURN_UNRESOLVED_REASON
    assert result.outcome == "error"
    assert not content.called and not tool_call.called


@pytest.mark.parametrize("fail_open", [None, False])
@respx.mock
async def test_no_verdict_denies_unless_every_org_policy_is_fail_open(fail_open):
    mock_srs(content=httpx.Response(503))

    result = await gate(TurnPolicy(policy_ids=("o1",), fail_open=fail_open, timeout_s=5)).evaluate(
        tool()
    )

    assert result.decision == PolicyDecision.DENY
    assert result.outcome == "error"
    assert result.reason == "SRS 503"


@respx.mock
async def test_no_verdict_allows_when_every_org_policy_is_fail_open():
    respx.post(INFERENCE).mock(side_effect=httpx.ConnectError("down"))

    result = await gate(TurnPolicy(policy_ids=("o1",), fail_open=True, timeout_s=5)).evaluate(
        tool()
    )

    assert result.decision == PolicyDecision.ALLOW
    assert result.outcome == "error"


@respx.mock
async def test_a_422_never_fails_open():
    """A body SRS rejected is our contract bug, not the outage an admin opted
    into failing open for."""
    mock_srs(tool_call=httpx.Response(422, json={"detail": [{"type": "missing"}]}))

    result = await gate(TurnPolicy(policy_ids=("o1",), fail_open=True, timeout_s=5)).evaluate(
        tool(args={"n": 1})
    )

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "SRS 422 (missing)"


@respx.mock
async def test_the_key_is_read_at_each_call():
    keys = iter(["first", "second"])
    content, _ = mock_srs()
    g = SrsToolGate(
        srs_url=SRS, api_key=lambda: next(keys), agent_name="a", engine_name="claude-agent-sdk"
    )
    g.set_turn(TURN)

    await g.evaluate(tool())
    await g.evaluate(tool())

    assert [c.request.headers["x-api-key"] for c in content.calls] == ["first", "second"]


@respx.mock
async def test_through_the_authorizer_a_deny_is_a_permission_deny():
    mock_srs(
        tool_call=httpx.Response(200, json={"allowed": False, "reason": "no", "denied_by": "opa"})
    )
    authorizer = PolicyToolAuthorizer(
        engine=gate(TURN), principal_resolver=lambda ctx: PolicyPrincipal(id="my-agent")
    )

    result = await authorizer("Bash", {"command": "ls"}, {"session_id": "s", "call_id": "c"})

    assert isinstance(result, PermissionResultDeny)
    assert result.message == "no"


@respx.mock
async def test_through_the_authorizer_an_allow_keeps_the_input():
    mock_srs()
    authorizer = PolicyToolAuthorizer(
        engine=gate(TURN), principal_resolver=lambda ctx: PolicyPrincipal(id="my-agent")
    )

    result = await authorizer("Bash", {"command": "ls"}, {"session_id": "s"})

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {"command": "ls"}
