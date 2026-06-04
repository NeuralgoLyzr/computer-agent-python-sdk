"""``AgentOSHttpSink`` — batches telemetry events and POSTs them to the AgentOS
ingest endpoint. Replaces the in-process Mongo sinks."""

from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from computeragent.telemetry.event import TelemetryEvent
from computeragent.telemetry.sinks import AgentOSHttpSink

URL = "http://agentos.test/agentos/api/ingest/events"


def _ev(kind: str, *, session_id: str = "sess_1", text: str = "hi", **payload):
    pl = dict(payload)
    if text is not None and kind in {"user_message", "assistant_message"}:
        pl.setdefault("text", text)
    return TelemetryEvent(
        kind=kind,  # type: ignore[arg-type]
        session_id=session_id,
        agent_name="QA-Bot",
        agent_description="Pytest specialist",
        payload=pl,
    )


# ── construction ─────────────────────────────────────────────────────────


def test_requires_url(monkeypatch):
    monkeypatch.delenv("AGENTOS_INGEST_URL", raising=False)
    with pytest.raises(ValueError, match="AGENTOS_INGEST_URL"):
        AgentOSHttpSink()


def test_url_from_env(monkeypatch):
    monkeypatch.setenv("AGENTOS_INGEST_URL", URL)
    sink = AgentOSHttpSink()
    assert sink._url == URL


# ── batching + drain triggers ──────────────────────────────────────────────


@respx.mock
async def test_flush_posts_buffered_batch_in_order():
    route = respx.post(URL).mock(return_value=Response(200, json={"ok": True}))
    sink = AgentOSHttpSink(url=URL)

    await sink.emit(_ev("user_message", text="first"))
    await sink.emit(_ev("assistant_message", text="second"))
    # Nothing should have been sent yet (below threshold, no session_ended).
    assert not route.called

    await sink.flush()
    assert route.called
    body = json.loads(route.calls.last.request.content)
    events = body["events"]
    assert [e["kind"] for e in events] == ["user_message", "assistant_message"]
    assert [e["payload"]["text"] for e in events] == ["first", "second"]
    await sink.aclose()


@respx.mock
async def test_session_ended_drains_immediately():
    route = respx.post(URL).mock(return_value=Response(200, json={"ok": True}))
    sink = AgentOSHttpSink(url=URL)

    await sink.emit(_ev("session_ended", text=None, is_error=False, result="done"))
    # session_ended forces a drain without an explicit flush().
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["events"][0]["kind"] == "session_ended"
    await sink.aclose()


@respx.mock
async def test_threshold_drains_without_flush():
    route = respx.post(URL).mock(return_value=Response(200, json={"ok": True}))
    sink = AgentOSHttpSink(url=URL, flush_threshold=2)

    await sink.emit(_ev("user_message", text="a"))
    assert not route.called
    await sink.emit(_ev("assistant_message", text="b"))
    # Hitting the threshold drains the buffer.
    assert route.called
    await sink.aclose()


# ── wire DTO shape ──────────────────────────────────────────────────────────


@respx.mock
async def test_dto_shape():
    route = respx.post(URL).mock(return_value=Response(200, json={"ok": True}))
    sink = AgentOSHttpSink(url=URL, host="pod-7")

    await sink.emit(_ev("tool_use", text=None, tool_name="Read", tool_input={"path": "x"}))
    await sink.flush()

    ev = json.loads(route.calls.last.request.content)["events"][0]
    assert set(ev) >= {
        "event_id",
        "kind",
        "session_id",
        "timestamp",
        "agent_name",
        "agent_description",
        "host",
        "payload",
    }
    assert ev["kind"] == "tool_use"
    assert ev["session_id"] == "sess_1"
    assert ev["agent_name"] == "QA-Bot"
    assert ev["agent_description"] == "Pytest specialist"
    assert ev["host"] == "pod-7"
    assert ev["payload"]["tool_name"] == "Read"
    # event_id is a non-empty stable id (the server keys idempotency on it).
    assert isinstance(ev["event_id"], str) and len(ev["event_id"]) >= 8
    await sink.aclose()


# ── auth ─────────────────────────────────────────────────────────────────────


@respx.mock
async def test_bearer_token_header_sent():
    route = respx.post(URL).mock(return_value=Response(200, json={"ok": True}))
    sink = AgentOSHttpSink(url=URL, token="secret-token")

    await sink.emit(_ev("user_message", text="q"))
    await sink.flush()
    assert route.calls.last.request.headers["authorization"] == "Bearer secret-token"
    await sink.aclose()


@respx.mock
async def test_no_token_no_auth_header():
    route = respx.post(URL).mock(return_value=Response(200, json={"ok": True}))
    sink = AgentOSHttpSink(url=URL)

    await sink.emit(_ev("user_message", text="q"))
    await sink.flush()
    assert "authorization" not in route.calls.last.request.headers
    await sink.aclose()


# ── never breaks the run ─────────────────────────────────────────────────────


@respx.mock
async def test_post_failure_is_swallowed_and_buffer_cleared():
    route = respx.post(URL).mock(return_value=Response(500, json={"error": "boom"}))
    sink = AgentOSHttpSink(url=URL)

    await sink.emit(_ev("user_message", text="q"))
    # A 5xx must not raise into the agent run.
    await sink.flush()
    assert route.call_count == 1
    # Buffer was cleared even on failure — a second flush is a no-op (no requeue
    # of a poisoned batch).
    await sink.flush()
    assert route.call_count == 1
    await sink.aclose()
