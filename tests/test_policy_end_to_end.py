"""End-to-end policy enforcement through the proxy + authorizer.

Wires a ``PolicyToolAuthorizer(engine=stub)`` into ``ClaudeAgentOptions`` and
exercises the full ``computeragent.query`` path. Verifies the authorizer is
invoked with the right arguments and that the agent's stream is unaffected
by the gating (we don't drive a real agent; we just confirm the wiring).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    ToolPermissionContext,
)

from computeragent import ClaudeAgentOptions, configure, query
from computeragent.policy import (
    PolicyDecision,
    PolicyInput,
    PolicyPrincipal,
    PolicyResource,
    PolicyResult,
    PolicyToolAuthorizer,
)


class _StubEngine:
    name = "stub"

    def __init__(self, decision: PolicyDecision):
        self.decision = decision
        self.last_input: PolicyInput | None = None

    async def evaluate(self, input: PolicyInput) -> PolicyResult:
        self.last_input = input
        return PolicyResult(decision=self.decision, reason="stub", engine=self.name)

    async def aclose(self) -> None:
        pass


def _result(text="done") -> Any:
    r = MagicMock(spec=ResultMessage)
    r.result = text
    r.total_cost_usd = 0.0
    r.num_turns = 1
    r.is_error = False
    r.subtype = "success"
    r.session_id = "sess_e2e"
    r.usage = {"input_tokens": 1, "output_tokens": 1}
    r.duration_api_ms = 0.0
    r.model = "stub"
    return r


@pytest.mark.asyncio
async def test_authorizer_wired_through_options_and_called_for_tool(
    upstream_query_factory, stub_sink
):
    """Simulate the engine calling the authorizer directly (the path claude-agent-sdk
    takes when ``can_use_tool`` is set). The authorizer must produce a valid
    PermissionResultAllow / PermissionResultDeny."""
    configure(sinks=[stub_sink])
    engine = _StubEngine(PolicyDecision.ALLOW)
    authorizer = PolicyToolAuthorizer(
        engine=engine,
        principal_resolver=lambda c: PolicyPrincipal(id="alice", groups=["engineer"]),
        resource_resolver=lambda c: PolicyResource(agent_name="nord", model="m"),
    )

    # Build options exactly as a user would.
    options = ClaudeAgentOptions(
        model="m",
        cwd="/tmp",
        system_prompt="You are NordAssist",
        allowed_tools=["Read"],
        permission_mode="default",
        can_use_tool=authorizer,
    )

    # Drive the proxied query with a trivial canned stream — we're testing
    # the wiring, not the agent.
    upstream_query_factory([_result()])
    msgs = [m async for m in query(prompt="hi", options=options)]
    assert msgs  # the stream got through; the proxy did not block on the authorizer

    # Now simulate claude-agent-sdk invoking can_use_tool — the same code
    # path that runs when a real agent emits a tool_use.
    mock_ctx = MagicMock(spec=ToolPermissionContext)
    mock_ctx.session_id = "sess_e2e"
    result = await options.can_use_tool("Read", {"path": "/x"}, mock_ctx)
    assert isinstance(result, PermissionResultAllow)
    assert engine.last_input is not None
    assert engine.last_input.action.tool_name == "Read"
    assert engine.last_input.principal.groups == ["engineer"]


@pytest.mark.asyncio
async def test_deny_decision_produces_permission_result_deny():
    engine = _StubEngine(PolicyDecision.DENY)
    authorizer = PolicyToolAuthorizer(
        engine=engine,
        principal_resolver=lambda c: PolicyPrincipal(id="alice"),
    )
    mock_ctx = MagicMock(spec=ToolPermissionContext)
    mock_ctx.session_id = "sess"
    result = await authorizer("Bash", {"command": "ls"}, mock_ctx)
    assert isinstance(result, PermissionResultDeny)
