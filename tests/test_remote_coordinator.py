"""ComputerAgent(harness_url=...) — the remote coordinator over both transports."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from computeragent import ComputerAgent
from computeragent.harness.errors import HarnessProtocolError

BASE = "http://remote.test"


def _sse(*events: dict) -> bytes:
    parts = [f"event: {ev['kind']}\ndata: {json.dumps(ev)}\n\n" for ev in events]
    return "".join(parts).encode()


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


# ── protocol transport (/v1) ──────────────────────────────────────────────────


def _mock_protocol(events: bytes) -> None:
    respx.post(f"{BASE}/v1/sessions").mock(
        return_value=httpx.Response(
            201,
            json={"sessionId": "s1", "engine": "claude-agent-sdk", "identity": {"name": "a", "version": "1"}, "capabilities": {}, "eventsUrl": "/v1/sessions/s1/events"},
        )
    )
    respx.post(f"{BASE}/v1/sessions/s1/messages").mock(return_value=httpx.Response(200, json={"ok": True}))
    respx.get(f"{BASE}/v1/sessions/s1/events").mock(return_value=httpx.Response(200, content=events))
    respx.delete(f"{BASE}/v1/sessions/s1").mock(return_value=httpx.Response(200, json={"ok": True}))


@respx.mock
async def test_protocol_full_chat_to_result():
    # Realistic streamingInput turn shape: usage lands on the wire BEFORE the
    # terminal `result` sdk_message (see engine.ts), and there is NO per-turn
    # `ca_session_ended` — the harness keeps the /events SSE open across turns.
    # The client must synthesize the terminator on the `result` and stop, or it
    # hangs forever on the open stream (the bug this guards).
    _mock_protocol(
        _sse(
            {"kind": "ca_session_started", "sessionId": "s1", "engine": "e", "identity": {}, "capabilities": {}},
            {"kind": "sdk_message", "sessionId": "s1", "payload": _assistant("hello there")},
            {"kind": "ca_usage_snapshot", "sessionId": "s1", "inputTokens": 10, "outputTokens": 5, "costUsd": 0.03, "costSemantic": "cumulative"},
            {"kind": "sdk_message", "sessionId": "s1", "payload": {"type": "result", "result": "hello there"}},
        )
    )
    async with ComputerAgent(
        harness_url=BASE, harness_kind="protocol", source={"type": "inline", "manifest": {"name": "a"}}, harness="claude-agent-sdk"
    ) as agent:
        handle = agent.chat("hi")
        # Worker-shape iteration: events carry .kind / .session_id / .reason.
        kinds = []
        async for ev in handle:
            kinds.append(ev.kind)
            if ev.kind == "ca_session_started":
                assert ev.session_id == "s1"
            if ev.kind == "ca_session_ended":
                assert ev.reason == "complete"
        result = await handle.result()

    assert kinds[0] == "ca_session_started" and kinds[-1] == "ca_session_ended"
    assert any(isinstance(m, dict) and m.get("type") == "result" for m in result.messages)
    assert result.usage.cost_usd == 0.03
    assert result.session_id


@respx.mock
async def test_protocol_multi_turn_reuses_session():
    create = respx.post(f"{BASE}/v1/sessions").mock(
        return_value=httpx.Response(201, json={"sessionId": "s1", "engine": "x", "identity": {"name": "a", "version": "1"}, "capabilities": {}, "eventsUrl": "/v1/sessions/s1/events"})
    )
    respx.post(f"{BASE}/v1/sessions/s1/messages").mock(return_value=httpx.Response(200, json={"ok": True}))
    respx.get(f"{BASE}/v1/sessions/s1/events").mock(
        return_value=httpx.Response(200, content=_sse({"kind": "ca_session_ended", "sessionId": "s1", "reason": "complete"}))
    )
    respx.delete(f"{BASE}/v1/sessions/s1").mock(return_value=httpx.Response(200, json={"ok": True}))

    async with ComputerAgent(harness_url=BASE, source={"type": "inline", "manifest": {"name": "a"}}, harness="claude-agent-sdk") as agent:
        await agent.chat("turn 1")
        await agent.chat("turn 2")
    # One session created, two messages posted.
    assert create.calls.call_count == 1


@respx.mock
async def test_protocol_session_ended_error_raises():
    _mock_protocol(
        _sse(
            {"kind": "ca_session_started", "sessionId": "s1", "engine": "e", "identity": {}, "capabilities": {}},
            {"kind": "ca_session_ended", "sessionId": "s1", "reason": "error", "errorMessage": "kaboom"},
        )
    )
    async with ComputerAgent(harness_url=BASE, source={"type": "inline", "manifest": {"name": "a"}}, harness="claude-agent-sdk") as agent:
        with pytest.raises(HarnessProtocolError, match="kaboom"):
            await agent.chat("hi")


# ── server transport (/sandboxes) ──────────────────────────────────────────────


@respx.mock
async def test_server_mode_forwards_runtime_and_collects_result():
    create = respx.post(f"{BASE}/sandboxes").mock(
        return_value=httpx.Response(201, json={"sandboxId": "sbx1", "sessionId": "ca_1"})
    )
    respx.post(f"{BASE}/sandboxes/sbx1/chat").mock(
        return_value=httpx.Response(
            200,
            content=_sse(
                {"kind": "ca_session_started", "sessionId": "ca_1", "engine": "e", "identity": {}, "capabilities": {}},
                {"kind": "sdk_message", "sessionId": "ca_1", "payload": {"type": "result", "result": "ok"}},
                {"kind": "ca_usage_snapshot", "sessionId": "ca_1", "inputTokens": 3, "outputTokens": 2, "costUsd": 0.01, "costSemantic": "cumulative"},
                {"kind": "ca_session_ended", "sessionId": "ca_1", "reason": "complete"},
                {"kind": "ca_done", "sessionId": "ca_1", "usage": {"costUsd": 0.01}},
            ),
        )
    )
    respx.delete(f"{BASE}/sandboxes/sbx1").mock(return_value=httpx.Response(200, json={"ok": True}))

    async with ComputerAgent(
        harness_url=BASE, harness_kind="server", runtime="bwrap", source={"type": "git", "url": "u"}, harness="claude-agent-sdk", model="claude-sonnet-4-5"
    ) as agent:
        result = await agent.chat("hi")

    body = json.loads(create.calls.last.request.content)
    assert body["runtime"] == "bwrap" and body["harness"] == "claude-agent-sdk" and body["model"] == "claude-sonnet-4-5"
    assert result.usage.cost_usd == 0.01
    assert any(isinstance(m, dict) and m.get("type") == "result" for m in result.messages)
