"""The app server's event stream, against CAS's ``sse-events.ts`` shapes."""

from __future__ import annotations

import asyncio

import pytest
import respx
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from computeragent.server.events import to_wire
from computeragent.telemetry import TelemetryEvent, get_pipeline
from server_helpers import FakeEngine, client_for, frames, make_app, mock_backends, session_body

# The fields each kind may carry (sse-events.ts); every event also has kind and sessionId.
ALLOWED = {
    "ca_session_started": {"engine", "identity", "capabilities"},
    "ca_turn_started": {"turnIndex", "message", "policyIds"},
    "ca_redaction_decision": {
        "stage",
        "outcome",
        "redacted",
        "blocked",
        "reason",
        "engine",
        "policyId",
        "policyIds",
        "latencyMs",
        "entityCounts",
        "locations",
    },
    "sdk_message": {"payload"},
    "ca_usage_snapshot": {
        "inputTokens",
        "outputTokens",
        "cacheCreationInputTokens",
        "cacheReadInputTokens",
        "costUsd",
        "costSemantic",
    },
    "ca_permission_request": {"callId", "toolName", "input", "risk"},
    "ca_permission_decision": {
        "callId",
        "decision",
        "reason",
        "toolName",
        "source",
        "deniedBy",
        "mode",
        "outcome",
        "policyId",
        "policyIds",
        "latencyMs",
    },
    "ca_session_ended": {"reason", "errorMessage", "error"},
}


def assert_schema(events):
    ids = [e[0] for e in events]
    assert ids == list(range(ids[0], ids[0] + len(ids)))
    for _, name, data in events:
        assert data["kind"] == name
        assert data["sessionId"] == "s1"
        assert set(data) - {"kind", "sessionId"} <= ALLOWED[name], name


async def run_turns(engine, *messages, **body):
    async with client_for(make_app(engine)) as client:
        await client.post("/v1/sessions", json=session_body(**body))
        for m in messages:
            await client.post(
                "/v1/sessions/s1/messages", json={"message": {"role": "user", "content": m}}
            )
        await client.post("/v1/sessions/s1/end-input")
        return frames((await client.get("/v1/sessions/s1/events")).text)


@respx.mock
async def test_turn_stream_matches_sse_events(agent_doc):
    mock_backends(agent_doc)

    events = await run_turns(FakeEngine(), "mail a@b.com")

    assert_schema(events)
    kinds = [e[1] for e in events]
    assert kinds == [
        "ca_session_started",
        "ca_turn_started",
        "ca_redaction_decision",
        "sdk_message",
        "ca_usage_snapshot",
        "sdk_message",
        "ca_session_ended",
    ]
    by = {name: data for _, name, data in events}
    assert by["ca_turn_started"]["turnIndex"] == 0
    assert by["ca_turn_started"]["message"] == {"role": "user", "content": "mail [EMAIL]"}
    redaction = by["ca_redaction_decision"]
    assert (
        redaction["stage"],
        redaction["outcome"],
        redaction["redacted"],
        redaction["blocked"],
    ) == ("llm_input", "redacted", True, False)
    assistant, result = events[3][2]["payload"], events[5][2]["payload"]
    assert assistant["type"] == "assistant"
    assert assistant["message"]["content"] == [{"type": "text", "text": "hello back"}]
    assert by["ca_usage_snapshot"] == {
        "kind": "ca_usage_snapshot",
        "sessionId": "s1",
        "inputTokens": 10,
        "outputTokens": 3,
        "costUsd": 0.01,
        "costSemantic": "cumulative",
    }
    assert result["type"] == "result" and result["result"] == "hello back"
    assert events[-1][2]["reason"] == "complete"


@respx.mock
async def test_blocked_input_ends_the_turn_with_a_policy_result(agent_doc):
    mock_backends(agent_doc)
    engine = FakeEngine()

    events = await run_turns(engine, "BLOCKME now", "fine")

    assert_schema(events)
    redactions = [e[2] for e in events if e[1] == "ca_redaction_decision"]
    assert redactions[0]["outcome"] == "blocked" and redactions[0]["blocked"] is True
    results = [
        e[2]["payload"]
        for e in events
        if e[1] == "sdk_message" and e[2]["payload"]["type"] == "result"
    ]
    assert results[0]["result"] == "blocked by policy"
    assert results[0]["ca_policy_blocked"] is True
    assert "ca_policy_blocked" not in results[1]
    assert [m["content"] for m in engine.messages] == ["fine"]
    assert [e[2]["turnIndex"] for e in events if e[1] == "ca_turn_started"] == [0, 1]


@respx.mock
async def test_an_engine_error_ends_the_session(agent_doc):
    mock_backends(agent_doc)

    events = await run_turns(FakeEngine(fail=True), "hi")

    assert_schema(events)
    ended = events[-1][2]
    assert ended["reason"] == "error"
    assert "engine exploded" in ended["errorMessage"]
    assert ended["error"]["severity"] == "error" and ended["error"]["retry"] is False


@respx.mock
async def test_permission_round_trip(agent_doc):
    mock_backends(agent_doc)
    engine = FakeEngine(ask_permission=True)
    async with client_for(make_app(engine)) as client:
        await client.post("/v1/sessions", json=session_body())
        await client.post(
            "/v1/sessions/s1/messages", json={"message": {"role": "user", "content": "write"}}
        )
        for _ in range(100):
            if client._transport.app.state.sessions["s1"].pending:  # type: ignore[attr-defined]
                break
            await asyncio.sleep(0.02)
        r = await client.post("/v1/sessions/s1/permission/nope", json={"decision": "allow"})
        assert r.status_code == 404 and r.json()["code"] == "PERMISSION_CALL_UNKNOWN"
        r = await client.post(
            "/v1/sessions/s1/permission/call_1",
            json={"decision": "modify", "input": {"path": "b.txt"}, "reason": "ok"},
        )
        assert r.json() == {"ok": True}
        await client.post("/v1/sessions/s1/end-input")
        events = frames((await client.get("/v1/sessions/s1/events")).text)

    assert_schema(events)
    request = next(e[2] for e in events if e[1] == "ca_permission_request")
    assert request["callId"] == "call_1" and request["toolName"] == "Write"
    decision = next(
        e[2] for e in events if e[1] == "ca_permission_decision" and e[2]["source"] == "client"
    )
    assert decision == {
        "kind": "ca_permission_decision",
        "sessionId": "s1",
        "callId": "call_1",
        "decision": "modify",
        "source": "client",
        "reason": "ok",
    }
    assert engine.decisions == [{"behavior": "allow", "updated_input": {"path": "b.txt"}}]


@respx.mock
async def test_a_tool_gate_decision_becomes_a_policy_permission_decision(agent_doc):
    mock_backends(agent_doc)
    async with client_for(make_app(FakeEngine())) as client:
        await client.post("/v1/sessions", json=session_body())
        await get_pipeline().emit(
            TelemetryEvent(
                kind="policy_decision",
                session_id="s1",
                payload={
                    "engine": "guardrails",
                    "decision": "deny",
                    "reason": "no rm",
                    "outcome": "blocked",
                    "policy_id": "pol_c",
                    "tool_name": "claude-agent-sdk__Bash",
                    "call_id": "toolu_1",
                    "latency_ms": 3.5,
                },
            )
        )
        await client.post("/v1/sessions/s1/end-input")
        events = frames((await client.get("/v1/sessions/s1/events")).text)

    assert_schema(events)
    decision = next(e[2] for e in events if e[1] == "ca_permission_decision")
    assert decision == {
        "kind": "ca_permission_decision",
        "sessionId": "s1",
        "callId": "toolu_1",
        "decision": "deny",
        "reason": "no rm",
        "toolName": "claude-agent-sdk__Bash",
        "source": "policy",
        "outcome": "blocked",
        "policyId": "pol_c",
        "latencyMs": 3.5,
    }


@respx.mock
async def test_last_event_id_replays_only_later_events(agent_doc):
    mock_backends(agent_doc)
    async with client_for(make_app(FakeEngine())) as client:
        await client.post("/v1/sessions", json=session_body())
        await client.post(
            "/v1/sessions/s1/messages", json={"message": {"role": "user", "content": "hi"}}
        )
        await client.post("/v1/sessions/s1/end-input")
        everything = frames((await client.get("/v1/sessions/s1/events")).text)
        later = frames(
            (await client.get("/v1/sessions/s1/events", headers={"Last-Event-ID": "2"})).text
        )
        by_query = frames((await client.get("/v1/sessions/s1/events?lastEventId=2")).text)

    assert [e[0] for e in later] == [e[0] for e in everything if e[0] > 2]
    assert by_query == later


# ── message conversion ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            AssistantMessage(
                content=[TextBlock(text="hi"), ToolUseBlock(id="t1", name="Bash", input={"c": 1})],
                model="m",
                parent_tool_use_id=None,
            ),
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "m",
                    "content": [
                        {"type": "text", "text": "hi"},
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"c": 1}},
                    ],
                },
            },
        ),
        (
            UserMessage(
                content=[ToolResultBlock(tool_use_id="t1", content="ok", is_error=False)],
                parent_tool_use_id="p1",
            ),
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "ok",
                            "is_error": False,
                        }
                    ],
                },
                "parent_tool_use_id": "p1",
            },
        ),
        (
            SystemMessage(subtype="init", data={"tools": ["Bash"]}),
            {"type": "system", "subtype": "init", "tools": ["Bash"]},
        ),
        (
            StreamEvent(
                uuid="u1", session_id="s", event={"type": "message_start"}, parent_tool_use_id=None
            ),
            {
                "type": "stream_event",
                "uuid": "u1",
                "session_id": "s",
                "event": {"type": "message_start"},
            },
        ),
        (
            {"role": "tool", "tool_call_id": "c1", "content": "out"},
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "out"}],
                },
            },
        ),
    ],
)
def test_to_wire(message, expected):
    assert to_wire(message) == expected


def test_to_wire_result_keeps_the_cli_field_names():
    wire = to_wire(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=2,
            session_id="s",
            total_cost_usd=0.5,
            result="done",
        )
    )
    assert {
        k: wire[k]
        for k in (
            "type",
            "subtype",
            "is_error",
            "num_turns",
            "session_id",
            "total_cost_usd",
            "result",
        )
    } == {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 2,
        "session_id": "s",
        "total_cost_usd": 0.5,
        "result": "done",
    }
