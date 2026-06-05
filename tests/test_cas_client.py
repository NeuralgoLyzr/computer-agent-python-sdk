"""ComputerAgent-server client — /health, /sandboxes, /sandboxes/:id/chat, /run."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from computeragent.harness.cas_client import ComputerAgentServerClient
from computeragent.harness.errors import HarnessProtocolError
from computeragent.harness.protocol import CaDone, CaError, CaSessionEnded

BASE = "http://cas.test"


def _sse(*events: dict) -> bytes:
    parts = [f"event: {ev['kind']}\ndata: {json.dumps(ev)}\n\n" for ev in events]
    return "".join(parts).encode()


@respx.mock
async def test_health_lists_runtimes():
    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(200, json={"ok": True, "runtimes": ["local", "bwrap"], "defaultRuntime": "local"})
    )
    c = ComputerAgentServerClient(base_url=BASE)
    h = await c.health()
    assert h["runtimes"] == ["local", "bwrap"]
    await c.aclose()


@respx.mock
async def test_create_sandbox_forwards_runtime():
    route = respx.post(f"{BASE}/sandboxes").mock(
        return_value=httpx.Response(201, json={"sandboxId": "sbx1", "sessionId": "ca_1"})
    )
    c = ComputerAgentServerClient(base_url=BASE)
    resp = await c.create_sandbox(
        {"source": {"type": "git", "url": "u"}, "harness": "claude-agent-sdk", "runtime": "bwrap", "sessionId": "ca_1"}
    )
    assert resp["sandboxId"] == "sbx1"
    body = json.loads(route.calls.last.request.content)
    assert body["runtime"] == "bwrap" and body["harness"] == "claude-agent-sdk"
    await c.aclose()


@respx.mock
async def test_chat_streams_events_with_ca_done_terminal():
    respx.post(f"{BASE}/sandboxes/sbx1/chat").mock(
        return_value=httpx.Response(
            200,
            content=_sse(
                {"kind": "ca_session_started", "sessionId": "ca_1", "engine": "e", "identity": {}, "capabilities": {}},
                {"kind": "sdk_message", "sessionId": "ca_1", "payload": {"type": "result", "result": "hi"}},
                {"kind": "ca_session_ended", "sessionId": "ca_1", "reason": "complete"},
                {"kind": "ca_done", "sessionId": "ca_1", "sandboxId": "sbx1", "turn": 1, "usage": {"costUsd": 0.02}},
            ),
        )
    )
    c = ComputerAgentServerClient(base_url=BASE)
    events = [ev async for ev in c.chat("sbx1", "hello")]
    kinds = [ev.kind for ev in events]
    assert kinds == ["ca_session_started", "sdk_message", "ca_session_ended", "ca_done"]
    assert isinstance(events[-1], CaDone) and events[-1].usage == {"costUsd": 0.02}
    assert isinstance(events[-2], CaSessionEnded)
    await c.aclose()


@respx.mock
async def test_chat_yields_ca_error():
    respx.post(f"{BASE}/sandboxes/sbx1/chat").mock(
        return_value=httpx.Response(200, content=_sse({"kind": "ca_error", "message": "blew up"}))
    )
    c = ComputerAgentServerClient(base_url=BASE)
    events = [ev async for ev in c.chat("sbx1", "hello")]
    assert len(events) == 1 and isinstance(events[0], CaError) and events[0].message == "blew up"
    await c.aclose()


@respx.mock
async def test_run_one_shot_streams():
    respx.post(f"{BASE}/run").mock(
        return_value=httpx.Response(
            200,
            content=_sse(
                {"kind": "ca_session_started", "sessionId": "ca_1", "engine": "e", "identity": {}, "capabilities": {}},
                {"kind": "ca_session_ended", "sessionId": "ca_1", "reason": "complete"},
                {"kind": "ca_done", "sessionId": "ca_1", "usage": {}},
            ),
        )
    )
    c = ComputerAgentServerClient(base_url=BASE)
    kinds = [ev.kind async for ev in c.run({"source": "u", "harness": "claude-agent-sdk", "message": "hi"})]
    assert kinds[0] == "ca_session_started" and kinds[-1] == "ca_done"
    await c.aclose()


@respx.mock
async def test_unknown_runtime_raises():
    respx.post(f"{BASE}/sandboxes").mock(
        return_value=httpx.Response(400, json={"error": {"code": "UNKNOWN_RUNTIME", "available": ["local"]}})
    )
    c = ComputerAgentServerClient(base_url=BASE)
    with pytest.raises(HarnessProtocolError, match="UNKNOWN_RUNTIME"):
        await c.create_sandbox({"source": "u", "harness": "x", "runtime": "nope"})
    await c.aclose()
