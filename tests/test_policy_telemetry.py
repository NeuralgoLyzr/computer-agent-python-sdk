"""Policy decisions emit a ``policy_decision`` telemetry event."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk.types import ToolPermissionContext

from computeragent import configure
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

    def __init__(self, decision: PolicyDecision, reason: str = ""):
        self._decision = decision
        self._reason = reason

    async def evaluate(self, input: PolicyInput) -> PolicyResult:
        return PolicyResult(decision=self._decision, reason=self._reason, engine=self.name)

    async def aclose(self) -> None:
        pass


def _ctx(session_id: str = "sess_abc") -> Any:
    mock = MagicMock(spec=ToolPermissionContext)
    mock.session_id = session_id
    return mock


@pytest.mark.asyncio
async def test_allow_emits_policy_decision_event(stub_sink):
    configure(sinks=[stub_sink])
    auth = PolicyToolAuthorizer(
        engine=_StubEngine(PolicyDecision.ALLOW, reason="ok"),
        principal_resolver=lambda c: PolicyPrincipal(id="alice", groups=["engineer"]),
        resource_resolver=lambda c: PolicyResource(agent_name="nord", model="m"),
    )
    await auth("Read", {"path": "/x"}, _ctx("sess_abc"))
    # Pipeline flushes pending sink tasks via the autouse fixture / aclose.
    # For the synchronous assertion path here, the sink should have received
    # the event during the await above.
    import asyncio

    await asyncio.sleep(0)  # yield once so background sink emit runs

    events = [e for e in stub_sink.events if e.kind == "policy_decision"]
    assert events, "expected a policy_decision event"
    payload = events[-1].payload
    assert payload["engine"] == "stub"
    assert payload["decision"] == "allow"
    assert payload["reason"] == "ok"
    assert payload["tool_name"] == "Read"
    assert payload["agent_name"] == "nord"
    assert payload["principal_id"] == "alice"
    assert payload["principal_groups"] == ["engineer"]
    assert isinstance(payload["latency_ms"], float)


@pytest.mark.asyncio
async def test_deny_emits_policy_decision_event(stub_sink):
    import asyncio

    configure(sinks=[stub_sink])
    auth = PolicyToolAuthorizer(
        engine=_StubEngine(PolicyDecision.DENY, reason="not allowed"),
        principal_resolver=lambda c: PolicyPrincipal(id="alice"),
    )
    await auth("Bash", {}, _ctx())
    await asyncio.sleep(0)

    events = [e for e in stub_sink.events if e.kind == "policy_decision"]
    assert events
    payload = events[-1].payload
    assert payload["decision"] == "deny"
    assert "not allowed" in payload["reason"]


@pytest.mark.asyncio
async def test_dict_context_carries_session_and_call_id(stub_sink):
    """The harness coordinator passes a plain dict context — session_id and
    call_id must still reach the event so the OTel sink can find the live
    execute_tool span (previously they were lost → session 'unknown')."""
    import asyncio

    configure(sinks=[stub_sink])
    auth = PolicyToolAuthorizer(
        engine=_StubEngine(PolicyDecision.DENY, reason="nope"),
        principal_resolver=lambda c: PolicyPrincipal(id="svc"),
    )
    await auth("Bash", {}, {"session_id": "ca_123", "call_id": "tu_7"})
    await asyncio.sleep(0)

    events = [e for e in stub_sink.events if e.kind == "policy_decision"]
    assert events
    ev = events[-1]
    assert ev.session_id == "ca_123"
    assert ev.payload["session_id"] == "ca_123"
    assert ev.payload["call_id"] == "tu_7"


@pytest.mark.asyncio
async def test_tool_permission_context_tool_use_id_becomes_call_id(stub_sink):
    """claude-agent-sdk's ToolPermissionContext carries tool_use_id (and the
    engine stamps session_id onto it) — both must ride the event."""
    import asyncio

    configure(sinks=[stub_sink])
    auth = PolicyToolAuthorizer(
        engine=_StubEngine(PolicyDecision.DENY, reason="blocked"),
        principal_resolver=lambda c: PolicyPrincipal(id="svc"),
    )
    ctx = _ctx("sess_live")
    ctx.tool_use_id = "toolu_abc"
    await auth("Write", {"path": "/etc/passwd"}, ctx)
    await asyncio.sleep(0)

    events = [e for e in stub_sink.events if e.kind == "policy_decision"]
    assert events
    ev = events[-1]
    assert ev.session_id == "sess_live"
    assert ev.payload["call_id"] == "toolu_abc"
