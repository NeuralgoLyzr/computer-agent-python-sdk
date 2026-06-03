"""``AgentRegistrySink`` — sessions.entries[] write for the AgentOS Chat tab.

These tests cover the v0.2 addition: in parallel with the existing
``agent_registry`` + ``agent_logs`` writes, the sink now also maintains a
``sessions`` doc with an ``entries[]`` array of ``{type, text}`` chat-bubble
messages, so the existing TS SPA Chat tab can render Python-driven sessions
without any frontend changes.
"""

from __future__ import annotations

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from computeragent.telemetry import TelemetryEvent  # noqa: E402
from computeragent.telemetry.sinks import AgentRegistrySink  # noqa: E402


def _ev(kind, payload=None, *, sid="sess_chat", agent="AcmeAssist"):
    return TelemetryEvent(kind=kind, session_id=sid, agent_name=agent, payload=payload or {})


@pytest.fixture
def mock_client():
    return AsyncMongoMockClient()


# ── opening + lifecycle ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_started_creates_sessions_doc(mock_client):
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("session_started", {"prompt": "hi", "model": "claude-sonnet-4-5"}))

    doc = await mock_client["agentos_test"]["sessions"].find_one({})
    assert doc is not None
    assert doc["_id"] == "sess_chat"
    assert doc["agentName"] == "AcmeAssist"
    assert doc["source"] == "library"
    assert doc["model"] == "claude-sonnet-4-5"
    assert isinstance(doc["entries"], list)
    # First entry is seeded from the prompt — that's turn 1 in the Chat tab.
    assert doc["entries"][0] == {"type": "user", "text": "hi"}


@pytest.mark.asyncio
async def test_assistant_message_appended_as_chat_bubble(mock_client):
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("session_started", {"prompt": "explain BM25", "model": "m"}))
    await sink.emit(_ev("assistant_message", {"text": "BM25 is a ranking function."}))

    doc = await mock_client["agentos_test"]["sessions"].find_one({})
    assert doc is not None
    # Entries are ordered: prompt seed, then the assistant reply.
    assert doc["entries"] == [
        {"type": "user", "text": "explain BM25"},
        {"type": "assistant", "text": "BM25 is a ranking function."},
    ]


@pytest.mark.asyncio
async def test_user_message_event_also_appends(mock_client):
    """Multi-turn: a follow-up `user_message` event (not the initial prompt)
    should append another `user` bubble."""
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("session_started", {"prompt": "first?", "model": "m"}))
    await sink.emit(_ev("assistant_message", {"text": "answer 1"}))
    await sink.emit(_ev("user_message", {"text": "second?"}))
    await sink.emit(_ev("assistant_message", {"text": "answer 2"}))

    doc = await mock_client["agentos_test"]["sessions"].find_one({})
    assert doc is not None
    assert [e["type"] for e in doc["entries"]] == ["user", "assistant", "user", "assistant"]
    assert doc["entries"][2]["text"] == "second?"


@pytest.mark.asyncio
async def test_tool_use_and_tool_result_not_appended(mock_client):
    """tool_use / tool_result / usage_snapshot / policy_decision are *not*
    chat bubbles — they're captured in agent_messages instead. The Chat tab
    only renders text messages."""
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("session_started", {"prompt": "do work", "model": "m"}))
    await sink.emit(_ev("tool_use", {"tool_name": "Read", "tool_use_id": "t1"}))
    await sink.emit(_ev("tool_result", {"tool_use_id": "t1", "content": "file body"}))
    await sink.emit(_ev("usage_snapshot", {"total_cost_usd": 0.01, "usage": {}}))
    await sink.emit(_ev("policy_decision", {"decision": "allow", "engine": "opa"}))
    await sink.emit(_ev("assistant_message", {"text": "done"}))

    doc = await mock_client["agentos_test"]["sessions"].find_one({})
    assert doc is not None
    # Only the seed user prompt + the final assistant reply.
    assert [e["type"] for e in doc["entries"]] == ["user", "assistant"]
    assert doc["entries"][0]["text"] == "do work"
    assert doc["entries"][1]["text"] == "done"


@pytest.mark.asyncio
async def test_session_ended_stamps_close_metadata(mock_client):
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("session_started", {"prompt": "hi", "model": "m"}))
    await sink.emit(_ev("assistant_message", {"text": "hello"}))
    await sink.emit(
        _ev(
            "session_ended",
            {
                "is_error": False,
                "result": "hello",
                "num_turns": 1,
                "total_cost_usd": 0.0042,
                "duration_ms": 500.0,
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        )
    )

    doc = await mock_client["agentos_test"]["sessions"].find_one({})
    assert doc is not None
    assert doc["ok"] is True
    assert doc["durationMs"] == 500
    assert abs(doc["costUsd"] - 0.0042) < 1e-9
    assert "endedAt" in doc


# ── slack_threads parity (drives sessionCount + lastActivity in /api/agents) ─


@pytest.mark.asyncio
async def test_slack_threads_row_written_on_session_started(mock_client):
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("session_started", {"prompt": "hi", "model": "m"}))

    thread = await mock_client["agentos_test"]["slack_threads"].find_one({"sessionId": "sess_chat"})
    assert thread is not None
    assert thread["bot"] == "AcmeAssist"
    assert thread["sessionId"] == "sess_chat"
    assert thread["channel"] == "library"  # default source value
    assert "createdAt" in thread
    assert "lastMessageAt" in thread


@pytest.mark.asyncio
async def test_slack_threads_lastMessageAt_updated_on_session_ended(mock_client):
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("session_started", {"prompt": "hi", "model": "m"}))
    before = await mock_client["agentos_test"]["slack_threads"].find_one({"sessionId": "sess_chat"})
    assert before is not None
    await sink.emit(_ev("session_ended", {"is_error": False, "result": "done"}))
    after = await mock_client["agentos_test"]["slack_threads"].find_one({"sessionId": "sess_chat"})
    assert after is not None
    assert after["lastMessageAt"] >= before["lastMessageAt"]


# ── opt-out ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_sessions_false_skips_sessions_collection_entirely(mock_client):
    sink = AgentRegistrySink(client=mock_client, database="agentos_test", write_sessions=False)
    await sink.emit(_ev("session_started", {"prompt": "hi", "model": "m"}))
    await sink.emit(_ev("assistant_message", {"text": "hello"}))
    await sink.emit(_ev("session_ended", {"is_error": False, "result": "hello", "usage": {}}))

    # No sessions doc was created; agent_logs still got the rollup.
    sessions_count = await mock_client["agentos_test"]["sessions"].count_documents({})
    logs_count = await mock_client["agentos_test"]["agent_logs"].count_documents({})
    assert sessions_count == 0
    assert logs_count == 1


# ── existing rollups still work alongside the new write ───────────────────


@pytest.mark.asyncio
async def test_agent_registry_and_logs_still_written(mock_client):
    """Sanity: adding the sessions write didn't break the existing
    agent_registry / agent_logs writes."""
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("session_started", {"prompt": "hi", "model": "m"}))
    await sink.emit(_ev("assistant_message", {"text": "ack"}))
    await sink.emit(_ev("session_ended", {"is_error": False, "result": "ack", "usage": {}}))

    reg = await mock_client["agentos_test"]["agent_registry"].find_one({"_id": "AcmeAssist"})
    logs = await mock_client["agentos_test"]["agent_logs"].find_one({})
    sess = await mock_client["agentos_test"]["sessions"].find_one({})
    assert reg is not None
    assert logs is not None
    assert sess is not None


# ── truncation safety ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_large_message_truncated_inside_sessions_entry(mock_client):
    """A 25 000-char assistant message must be capped so the sessions doc
    can't blow past Mongo's 16 MB ceiling on a long-running session."""
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("session_started", {"prompt": "p", "model": "m"}))
    big = "Z" * 25_000
    await sink.emit(_ev("assistant_message", {"text": big}))

    doc = await mock_client["agentos_test"]["sessions"].find_one({})
    assert doc is not None
    # REPLY_MAX is 16_000 — see agent-registry-mongo TS package; we mirror it.
    assert len(doc["entries"][1]["text"]) <= 16_000


# ── idempotency on session_started ────────────────────────────────────────


@pytest.mark.asyncio
async def test_repeat_session_started_does_not_duplicate_seed(mock_client):
    """If the proxy calls session_started twice in one process for the same
    session id (e.g. a resumed session), we should not push the seed prompt
    twice."""
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("session_started", {"prompt": "hi", "model": "m"}))
    # Second time — already opened, in-memory guard kicks in.
    await sink.emit(_ev("session_started", {"prompt": "hi", "model": "m"}))

    doc = await mock_client["agentos_test"]["sessions"].find_one({})
    assert doc is not None
    user_entries = [e for e in doc["entries"] if e["type"] == "user"]
    # Seed prompt was only appended once.
    assert len(user_entries) == 1
