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


# ── API-key token threading (COMPUTERAGENT_HARNESS_TOKEN) ─────────────────────


def test_harness_token_explicit_arg(monkeypatch):
    monkeypatch.delenv("COMPUTERAGENT_HARNESS_TOKEN", raising=False)
    agent = ComputerAgent(
        harness_url=BASE, source={"type": "inline", "manifest": {"name": "a"}}, harness_token="cak_explicit"
    )
    assert agent._coordinator._token == "cak_explicit"


def test_harness_token_env_fallback(monkeypatch):
    monkeypatch.setenv("COMPUTERAGENT_HARNESS_TOKEN", "cak_env")
    agent = ComputerAgent(harness_url=BASE, source={"type": "inline", "manifest": {"name": "a"}})
    assert agent._coordinator._token == "cak_env"


def test_harness_token_explicit_wins_over_env(monkeypatch):
    monkeypatch.setenv("COMPUTERAGENT_HARNESS_TOKEN", "cak_env")
    agent = ComputerAgent(
        harness_url=BASE, source={"type": "inline", "manifest": {"name": "a"}}, harness_token="cak_explicit"
    )
    assert agent._coordinator._token == "cak_explicit"


def test_harness_token_none_when_unset(monkeypatch):
    monkeypatch.delenv("COMPUTERAGENT_HARNESS_TOKEN", raising=False)
    agent = ComputerAgent(harness_url=BASE, source={"type": "inline", "manifest": {"name": "a"}})
    assert agent._coordinator._token is None


# ── stable agent_id resolution (explicit kwarg / env / fallback) ─────────────


def test_agent_id_explicit_arg(monkeypatch):
    monkeypatch.delenv("COMPUTERAGENT_AGENT_ID", raising=False)
    monkeypatch.setenv("AGENTOS_INGEST_URL", "http://x/ingest")  # agent_id requires ingest
    agent = ComputerAgent(
        harness_url=BASE, source={"type": "inline", "manifest": {"name": "a"}}, agent_id="aid-explicit"
    )
    assert agent._coordinator._agent_id == "aid-explicit"


def test_agent_id_env_fallback(monkeypatch):
    monkeypatch.setenv("COMPUTERAGENT_AGENT_ID", "aid-env")
    monkeypatch.setenv("AGENTOS_INGEST_URL", "http://x/ingest")
    agent = ComputerAgent(harness_url=BASE, source={"type": "inline", "manifest": {"name": "a"}})
    assert agent._coordinator._agent_id == "aid-env"


def test_agent_id_explicit_wins_over_env(monkeypatch):
    monkeypatch.setenv("COMPUTERAGENT_AGENT_ID", "aid-env")
    monkeypatch.setenv("AGENTOS_INGEST_URL", "http://x/ingest")
    agent = ComputerAgent(
        harness_url=BASE, source={"type": "inline", "manifest": {"name": "a"}}, agent_id="aid-explicit"
    )
    assert agent._coordinator._agent_id == "aid-explicit"


def test_agent_id_none_when_unset(monkeypatch):
    monkeypatch.delenv("COMPUTERAGENT_AGENT_ID", raising=False)
    agent = ComputerAgent(harness_url=BASE, source={"type": "inline", "manifest": {"name": "a"}})
    assert agent._coordinator._agent_id is None


async def test_agent_id_stamped_on_emitted_event(monkeypatch):
    # The coordinator's emit chokepoint stamps agent_id onto every event so the
    # AgentOS ingest can key the registry/joins on it.
    from computeragent.telemetry.event import TelemetryEvent

    monkeypatch.delenv("COMPUTERAGENT_AGENT_ID", raising=False)
    monkeypatch.setenv("AGENTOS_INGEST_URL", "http://x/ingest")  # agent_id requires ingest
    agent = ComputerAgent(
        harness_url=BASE, source={"type": "inline", "manifest": {"name": "a"}}, agent_id="aid-1"
    )
    captured: list[TelemetryEvent] = []

    async def _fake_emit(ev: TelemetryEvent) -> None:
        captured.append(ev)

    agent._coordinator._pipeline.emit = _fake_emit  # type: ignore[assignment]
    ev = TelemetryEvent(kind="user_message", session_id="s", agent_name="display-name")
    await agent._coordinator._emit_safe(ev)
    assert captured and captured[0].agent_id == "aid-1"


def test_agentos_http_sink_serializes_agent_id():
    # The wire dto carries agent_id alongside agent_name.
    from computeragent.telemetry.event import TelemetryEvent
    from computeragent.telemetry.sinks import AgentOSHttpSink

    # Pass a dummy client so no real httpx.AsyncClient is created/leaked.
    sink = AgentOSHttpSink(url="http://x/ingest", client=object())
    dto = sink._serialize(TelemetryEvent(kind="session_started", session_id="s", agent_name="n", agent_id="aid-1"))
    assert dto["agent_id"] == "aid-1"
    assert dto["agent_name"] == "n"


# ── agent_id: ingest enforcement + run-by-reference resolution ───────────────


def test_agent_id_requires_ingest(monkeypatch):
    # agent_id set + no AGENTOS_INGEST_URL + no pipeline → raise (no event-loss hole).
    monkeypatch.delenv("AGENTOS_INGEST_URL", raising=False)
    monkeypatch.delenv("COMPUTERAGENT_AGENT_ID", raising=False)
    with pytest.raises(ValueError, match="no AgentOS ingest"):
        ComputerAgent(source={"type": "inline", "manifest": {"name": "a"}}, agent_id="aid-x")


def test_agent_id_explicit_pipeline_satisfies_ingest(monkeypatch):
    # An explicit pipeline counts as ingest configured → no raise.
    from computeragent.telemetry import Pipeline

    monkeypatch.delenv("AGENTOS_INGEST_URL", raising=False)
    agent = ComputerAgent(
        source={"type": "inline", "manifest": {"name": "a"}},
        agent_id="aid-x",
        telemetry_pipeline=Pipeline(sinks=[]),
    )
    assert agent._coordinator._agent_id == "aid-x"


def test_agent_id_resolves_source_when_omitted(monkeypatch):
    # agent_id + no source → fetch source from AgentOS (local mode).
    import computeragent.harness.agent_resolve_client as arc

    monkeypatch.setenv("AGENTOS_INGEST_URL", "http://x/ingest")
    monkeypatch.setenv("AGENTOS_API_URL", "http://x/agentos/api/v1")
    monkeypatch.setenv("COMPUTERAGENT_HARNESS_TOKEN", "cak_x")
    monkeypatch.setattr(
        arc, "resolve_agent",
        lambda *a, **k: {"name": "n", "source": "github.com/org/repo", "harness": "claude-agent-sdk", "model": "claude-y"},
    )
    agent = ComputerAgent(agent_id="aid-x")  # no source → resolved
    assert agent._coordinator._source == "github.com/org/repo"


def test_agent_id_source_passed_skips_resolve(monkeypatch):
    # When source IS given, the caller is authoritative — no resolve call.
    import computeragent.harness.agent_resolve_client as arc

    monkeypatch.setenv("AGENTOS_INGEST_URL", "http://x/ingest")
    calls = {"n": 0}

    def _resolve(*a, **k):
        calls["n"] += 1
        return

    monkeypatch.setattr(arc, "resolve_agent", _resolve)
    ComputerAgent(source={"type": "inline", "manifest": {"name": "a"}}, agent_id="aid-x")
    assert calls["n"] == 0


def test_agent_id_unregistered_raises(monkeypatch):
    # agent_id + no source + resolve miss → hard error (run-existing only).
    import computeragent.harness.agent_resolve_client as arc

    monkeypatch.setenv("AGENTOS_INGEST_URL", "http://x/ingest")
    monkeypatch.setenv("AGENTOS_API_URL", "http://x/agentos/api/v1")
    monkeypatch.setenv("COMPUTERAGENT_HARNESS_TOKEN", "cak_x")
    monkeypatch.setattr(arc, "resolve_agent", lambda *a, **k: None)
    with pytest.raises(ValueError, match="not registered"):
        ComputerAgent(agent_id="aid-x")


def test_no_source_no_agent_id_raises(monkeypatch):
    monkeypatch.delenv("COMPUTERAGENT_AGENT_ID", raising=False)
    with pytest.raises(ValueError, match="source or agent_id"):
        ComputerAgent()


@respx.mock
async def test_harness_token_sent_as_bearer(monkeypatch):
    # End-to-end: the resolved token reaches the wire as `Authorization: Bearer`.
    monkeypatch.setenv("COMPUTERAGENT_HARNESS_TOKEN", "cak_env")
    create = respx.post(f"{BASE}/v1/sessions").mock(
        return_value=httpx.Response(
            201,
            json={"sessionId": "s1", "engine": "e", "identity": {"name": "a", "version": "1"}, "capabilities": {}, "eventsUrl": "/v1/sessions/s1/events"},
        )
    )
    respx.post(f"{BASE}/v1/sessions/s1/messages").mock(return_value=httpx.Response(200, json={"ok": True}))
    respx.get(f"{BASE}/v1/sessions/s1/events").mock(
        return_value=httpx.Response(200, content=_sse({"kind": "sdk_message", "sessionId": "s1", "payload": {"type": "result", "result": "ok"}}))
    )
    respx.delete(f"{BASE}/v1/sessions/s1").mock(return_value=httpx.Response(200, json={"ok": True}))

    async with ComputerAgent(
        harness_url=BASE, harness_kind="protocol", source={"type": "inline", "manifest": {"name": "a"}}, harness="claude-agent-sdk"
    ) as agent:
        await agent.chat("hi")

    assert create.calls.last.request.headers["authorization"] == "Bearer cak_env"
