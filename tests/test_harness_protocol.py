"""Harness Protocol wire layer — DTO parsing, SSE parser, and the /v1 client."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from computeragent.harness.errors import HarnessProtocolError
from computeragent.harness.protocol import (
    CaPermissionRequest,
    CaSessionEnded,
    CaUsageSnapshot,
    HarnessClient,
    SdkMessage,
    parse_event,
    parse_sse,
    user_message,
)

BASE = "http://harness.test"


def _sse(*events: dict) -> bytes:
    """Encode events as an SSE stream (event/id/data blocks)."""
    parts = []
    for i, ev in enumerate(events):
        parts.append(f"event: {ev['kind']}\nid: {i}\ndata: {json.dumps(ev)}\n\n")
    return "".join(parts).encode()


async def _aiter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


# ── parse_event ──────────────────────────────────────────────────────────────


def test_parse_event_maps_camel_to_snake():
    ev = parse_event({"kind": "ca_session_ended", "sessionId": "s1", "reason": "error", "errorMessage": "boom"})
    assert isinstance(ev, CaSessionEnded)
    assert ev.session_id == "s1" and ev.reason == "error" and ev.error_message == "boom"

    pe = parse_event({"kind": "ca_permission_request", "sessionId": "s1", "callId": "c1", "toolName": "Bash", "input": {"x": 1}, "risk": "high"})
    assert isinstance(pe, CaPermissionRequest)
    assert pe.call_id == "c1" and pe.tool_name == "Bash" and pe.risk == "high" and pe.input == {"x": 1}

    us = parse_event({"kind": "ca_usage_snapshot", "sessionId": "s1", "inputTokens": 10, "outputTokens": 5, "costUsd": 0.01, "costSemantic": "cumulative"})
    assert isinstance(us, CaUsageSnapshot)
    assert us.input_tokens == 10 and us.output_tokens == 5 and us.cost_usd == 0.01 and us.cost_semantic == "cumulative"

    sm = parse_event({"kind": "sdk_message", "sessionId": "s1", "payload": {"type": "assistant"}})
    assert isinstance(sm, SdkMessage) and sm.payload == {"type": "assistant"}


def test_parse_event_unknown_kind_is_tolerated():
    ev = parse_event({"kind": "ca_future_thing", "sessionId": "s1", "extra": 1})
    assert ev.kind == "ca_future_thing" and ev.session_id == "s1"


def test_user_message_normalizes():
    assert user_message("hi") == {"role": "user", "content": "hi"}
    assert user_message({"role": "user", "content": "x"}) == {"role": "user", "content": "x"}


# ── SSE parser ────────────────────────────────────────────────────────────────


async def test_parse_sse_handles_partial_chunks_and_keepalive():
    raw = _sse(
        {"kind": "ca_session_started", "sessionId": "s1"},
        {"kind": "ca_session_ended", "sessionId": "s1", "reason": "complete"},
    )
    # Inject a keepalive comment + split the stream mid-block across chunks.
    raw = raw[:20] + b":keepalive\n\n" + raw[20:]
    chunks = [raw[:15], raw[15:40], raw[40:]]
    out = [(eid, obj) async for eid, obj in parse_sse(_aiter(chunks))]
    assert [o["kind"] for _, o in out] == ["ca_session_started", "ca_session_ended"]
    assert out[1][0] == 1  # id: tracked


async def test_parse_sse_skips_malformed_json():
    raw = b"event: x\ndata: {not json}\n\n" + _sse({"kind": "ca_session_ended", "sessionId": "s1", "reason": "complete"})
    out = [obj async for _eid, obj in parse_sse(_aiter([raw]))]
    assert [o["kind"] for o in out] == ["ca_session_ended"]


# ── HarnessClient (/v1) ───────────────────────────────────────────────────────


@respx.mock
async def test_create_session_send_message_decide_permission():
    create = respx.post(f"{BASE}/v1/sessions").mock(
        return_value=httpx.Response(201, json={"sessionId": "s1", "engine": "claude-agent-sdk", "identity": {"name": "a", "version": "1"}, "capabilities": {}, "eventsUrl": "/v1/sessions/s1/events"})
    )
    msg = respx.post(f"{BASE}/v1/sessions/s1/messages").mock(return_value=httpx.Response(200, json={"ok": True}))
    perm = respx.post(f"{BASE}/v1/sessions/s1/permission/c1").mock(return_value=httpx.Response(200, json={"ok": True}))

    c = HarnessClient(base_url=BASE)
    resp = await c.create_session({"engine": "claude-agent-sdk", "identity": {"loader": "gitagentprotocol", "source": {"type": "git", "url": "u"}}})
    assert resp["sessionId"] == "s1"
    assert json.loads(create.calls.last.request.content)["engine"] == "claude-agent-sdk"

    await c.send_message("s1", user_message("hello"))
    assert json.loads(msg.calls.last.request.content)["message"] == {"role": "user", "content": "hello"}

    await c.decide_permission("s1", "c1", "allow")
    assert json.loads(perm.calls.last.request.content)["decision"] == "allow"
    await c.aclose()


@respx.mock
async def test_stream_events_yields_parsed_events_in_order():
    respx.get(f"{BASE}/v1/sessions/s1/events").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"kind": "ca_session_started", "sessionId": "s1", "engine": "e", "identity": {}, "capabilities": {}},
                {"kind": "sdk_message", "sessionId": "s1", "payload": {"type": "assistant"}},
                {"kind": "ca_usage_snapshot", "sessionId": "s1", "inputTokens": 1, "outputTokens": 2, "costUsd": 0.5, "costSemantic": "cumulative"},
                {"kind": "ca_session_ended", "sessionId": "s1", "reason": "complete"},
            ),
        )
    )
    c = HarnessClient(base_url=BASE)
    events = [(eid, ev) async for eid, ev in c.stream_events("s1")]
    kinds = [ev.kind for _eid, ev in events]
    assert kinds == ["ca_session_started", "sdk_message", "ca_usage_snapshot", "ca_session_ended"]
    assert isinstance(events[-1][1], CaSessionEnded)
    assert events[-1][0] == 3  # last id
    await c.aclose()


@respx.mock
async def test_resume_sends_last_event_id_header():
    route = respx.get(f"{BASE}/v1/sessions/s1/events").mock(
        return_value=httpx.Response(200, content=_sse({"kind": "ca_session_ended", "sessionId": "s1", "reason": "complete"}))
    )
    c = HarnessClient(base_url=BASE)
    _ = [ev async for _eid, ev in c.stream_events("s1", last_event_id=42)]
    assert route.calls.last.request.headers.get("last-event-id") == "42"
    await c.aclose()


@respx.mock
async def test_http_error_raises_harness_protocol_error():
    respx.post(f"{BASE}/v1/sessions").mock(return_value=httpx.Response(500, json={"error": "boom"}))
    c = HarnessClient(base_url=BASE)
    with pytest.raises(HarnessProtocolError):
        await c.create_session({"engine": "x", "identity": {"loader": "l", "source": "s"}})
    await c.aclose()


def test_bearer_token_header():
    c = HarnessClient(base_url=BASE, token="tok")
    assert c._client.headers.get("authorization") == "Bearer tok"
