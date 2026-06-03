"""Canonical policy types — construction, JSON round-trip, identity."""

from __future__ import annotations

from computeragent.policy import (
    PolicyAction,
    PolicyDecision,
    PolicyInput,
    PolicyPrincipal,
    PolicyResource,
    PolicyResult,
)


def test_principal_to_json_includes_defaults():
    p = PolicyPrincipal(id="alice")
    assert p.to_json() == {"id": "alice", "groups": [], "attributes": {}}


def test_action_to_json():
    a = PolicyAction(name="tool_use", tool_name="Read", tool_input={"path": "/tmp/x"})
    assert a.to_json() == {
        "name": "tool_use",
        "tool_name": "Read",
        "tool_input": {"path": "/tmp/x"},
    }


def test_resource_to_json_with_session():
    r = PolicyResource(agent_name="acme-assist", model="claude-sonnet-4-5", session_id="s1")
    assert r.to_json()["agent_name"] == "acme-assist"
    assert r.to_json()["session_id"] == "s1"
    assert r.to_json()["type"] == "agent"


def test_input_to_json_is_full_shape():
    inp = PolicyInput(
        principal=PolicyPrincipal(id="alice", groups=["engineer"]),
        action=PolicyAction(name="tool_use", tool_name="Bash", tool_input={"command": "ls"}),
        resource=PolicyResource(agent_name="acme", model="m", session_id="s"),
        context={"env": "prod"},
    )
    j = inp.to_json()
    # Same shape OPA receives over the wire.
    assert j == {
        "principal": {"id": "alice", "groups": ["engineer"], "attributes": {}},
        "action": {"name": "tool_use", "tool_name": "Bash", "tool_input": {"command": "ls"}},
        "resource": {
            "type": "agent",
            "agent_name": "acme",
            "model": "m",
            "session_id": "s",
            "attributes": {},
        },
        "context": {"env": "prod"},
    }


def test_result_carries_engine_name():
    r = PolicyResult(decision=PolicyDecision.ALLOW, reason="ok", engine="opa")
    assert r.decision == PolicyDecision.ALLOW
    assert r.engine == "opa"
    assert r.obligations == []
