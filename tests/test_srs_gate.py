"""The guardrails gate on every tool call.

One ``tool-input`` check per call through the egress proxy, carrying the
engine's tool-use id; the gateway owns policy selection and fail-open.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

from computeragent.policy import PolicyToolAuthorizer
from computeragent.policy.srs_gate import NO_CALL_ID_REASON, SrsToolGate
from computeragent.policy.types import (
    PolicyAction,
    PolicyDecision,
    PolicyInput,
    PolicyPrincipal,
    PolicyResource,
)

GUARDRAILS = "http://egress.internal/guardrails"
CHECK = f"{GUARDRAILS}/v1/check"
SRS_PATHS = ("/v1/rai/inference", "/v1/guardrails/evaluate-tool-call")
KEY = "sbt_key"


def answer(decision="allow", **data):
    return httpx.Response(200, json={"data": {"decision": decision, "op": "tool-input", **data}})


def gate(*, engine="claude-agent-sdk", mcp=(), api_key=lambda: KEY):
    return SrsToolGate(
        guardrails_url=GUARDRAILS, api_key=api_key, engine_name=engine, mcp_server_names=mcp
    )


def tool(name="Bash", args=None, call_id="toolu_1", session="sess-1"):
    context = {"session_id": session}
    if call_id is not None:
        context["call_id"] = call_id
    return PolicyInput(
        principal=PolicyPrincipal(id="my-agent"),
        action=PolicyAction(
            name="tool_use",
            tool_name=name,
            tool_input={"command": "rm -rf /tmp/x"} if args is None else args,
        ),
        resource=PolicyResource(type="agent", agent_name="my-agent", session_id=session),
        context=context,
    )


def sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_one_tool_input_check_with_the_engines_id():
    route = respx.post(CHECK).mock(return_value=answer())

    result = await gate().evaluate(tool(args={"command": "ls"}))

    assert result.decision == PolicyDecision.ALLOW
    assert result.outcome == "allowed"
    assert route.call_count == 1
    assert sent(route) == {
        "op": "tool-input",
        "subject": {
            "toolCalls": [
                {"id": "toolu_1", "name": "claude-agent-sdk__Bash", "arguments": {"command": "ls"}}
            ]
        },
    }
    assert route.calls.last.request.headers["x-api-key"] == KEY


@respx.mock
async def test_srs_and_org_policy_routes_are_never_called():
    route = respx.post(CHECK).mock(return_value=answer())
    others = respx.route().mock(return_value=httpx.Response(599))

    await gate().evaluate(tool())

    assert route.called
    assert not others.called
    assert not any(p in str(c.request.url) for c in route.calls for p in SRS_PATHS)


@respx.mock
async def test_a_gitagent_mcp_tool_gets_the_canonical_name():
    route = respx.post(CHECK).mock(return_value=answer())

    await gate(engine="gitagent", mcp=["jira"]).evaluate(tool("jira__search"))

    assert sent(route)["subject"]["toolCalls"][0]["name"] == "mcp__jira__search"


@pytest.mark.parametrize("call_id", [None, ""])
@respx.mock
async def test_tool_call_without_id_is_denied(call_id):
    route = respx.post(CHECK).mock(return_value=answer())

    result = await gate().evaluate(tool(call_id=call_id))

    assert result.decision == PolicyDecision.DENY
    assert result.reason == NO_CALL_ID_REASON
    assert result.outcome == "error"
    assert not route.called


@respx.mock
async def test_a_block_names_the_policy_that_acted():
    respx.post(CHECK).mock(
        return_value=answer(
            "block",
            results=[
                {
                    "id": "toolu_1",
                    "decision": "block",
                    "deniedBy": "cedar",
                    "reason": "rm is forbidden",
                    "policyId": "org1",
                }
            ],
        )
    )

    result = await gate().evaluate(tool())

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "rm is forbidden"
    assert result.policy_id == "org1"
    assert result.outcome == "blocked"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, json={"error": {"code": "rai_unavailable"}}),
        httpx.Response(400, json={"error": {"code": "invalid_request"}}),
        httpx.Response(413),
    ],
)
@respx.mock
async def test_no_verdict_denies(response):
    respx.post(CHECK).mock(return_value=response)

    result = await gate().evaluate(tool())

    assert result.decision == PolicyDecision.DENY
    assert result.outcome == "error"
    assert result.reason.startswith(f"guardrails {response.status_code}")


@respx.mock
async def test_unreachable_denies():
    respx.post(CHECK).mock(side_effect=httpx.ConnectError("down"))

    result = await gate().evaluate(tool())

    assert result.decision == PolicyDecision.DENY
    assert result.reason == "guardrails unreachable: ConnectError"


@respx.mock
async def test_the_key_is_read_at_each_call():
    keys = iter(["first", "second"])
    route = respx.post(CHECK).mock(return_value=answer())
    g = gate(api_key=lambda: next(keys))

    await g.evaluate(tool())
    await g.evaluate(tool())

    assert [c.request.headers["x-api-key"] for c in route.calls] == ["first", "second"]


@respx.mock
async def test_through_the_authorizer_the_id_reaches_the_check_and_a_deny_is_a_permission_deny():
    route = respx.post(CHECK).mock(
        return_value=answer("block", results=[{"decision": "block", "reason": "no"}])
    )
    authorizer = PolicyToolAuthorizer(
        engine=gate(), principal_resolver=lambda ctx: PolicyPrincipal(id="my-agent")
    )

    result = await authorizer("Bash", {"command": "ls"}, {"session_id": "s", "tool_use_id": "tu_9"})

    assert isinstance(result, PermissionResultDeny)
    assert result.message == "no"
    assert sent(route)["subject"]["toolCalls"][0]["id"] == "tu_9"


@respx.mock
async def test_through_the_authorizer_an_allow_keeps_the_input():
    respx.post(CHECK).mock(return_value=answer())
    authorizer = PolicyToolAuthorizer(
        engine=gate(), principal_resolver=lambda ctx: PolicyPrincipal(id="my-agent")
    )

    result = await authorizer("Bash", {"command": "ls"}, {"session_id": "s", "call_id": "c"})

    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {"command": "ls"}


async def test_through_the_authorizer_no_id_is_a_permission_deny():
    authorizer = PolicyToolAuthorizer(
        engine=gate(), principal_resolver=lambda ctx: PolicyPrincipal(id="my-agent")
    )

    result = await authorizer("Bash", {"command": "ls"}, {"session_id": "s"})

    assert isinstance(result, PermissionResultDeny)
    assert result.message == NO_CALL_ID_REASON
