"""``MongoMessageSink`` — per-message archival into Mongo ``agent_messages``."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from computeragent import PiiRedactor, Pipeline  # noqa: E402
from computeragent.telemetry import TelemetryEvent  # noqa: E402
from computeragent.telemetry.sinks import MongoMessageSink  # noqa: E402


def _ev(kind, payload=None, *, sid="sess_msg", agent="NordAssist"):
    return TelemetryEvent(
        kind=kind,
        session_id=sid,
        agent_name=agent,
        payload=payload or {},
        timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def mock_client():
    return AsyncMongoMockClient()


# ── per-kind writes ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assistant_message_archived(mock_client):
    sink = MongoMessageSink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("assistant_message", {"text": "hello", "model": "m"}))

    docs = await mock_client["agentos_test"]["agent_messages"].find().to_list(length=10)
    assert len(docs) == 1
    d = docs[0]
    assert d["_id"].startswith("msg_")
    assert d["kind"] == "assistant_message"
    assert d["sessionId"] == "sess_msg"
    assert d["agentName"] == "NordAssist"
    assert d["payload"]["text"] == "hello"
    assert d["payload"]["model"] == "m"
    assert d["source"] == "library"


@pytest.mark.asyncio
async def test_tool_use_archived_with_input(mock_client):
    sink = MongoMessageSink(client=mock_client, database="agentos_test")
    await sink.emit(
        _ev("tool_use", {"tool_name": "Read", "tool_use_id": "t1", "tool_input": {"path": "/x"}})
    )

    d = await mock_client["agentos_test"]["agent_messages"].find_one({})
    assert d is not None
    assert d["kind"] == "tool_use"
    assert d["payload"]["tool_name"] == "Read"
    assert d["payload"]["tool_input"] == {"path": "/x"}


@pytest.mark.asyncio
async def test_tool_result_archived(mock_client):
    sink = MongoMessageSink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("tool_result", {"tool_use_id": "t1", "is_error": False, "content": "ok"}))

    d = await mock_client["agentos_test"]["agent_messages"].find_one({})
    assert d is not None
    assert d["kind"] == "tool_result"
    assert d["payload"]["tool_use_id"] == "t1"


@pytest.mark.asyncio
async def test_usage_snapshot_archived(mock_client):
    sink = MongoMessageSink(client=mock_client, database="agentos_test")
    await sink.emit(
        _ev(
            "usage_snapshot",
            {
                "model": "m",
                "total_cost_usd": 0.05,
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )
    )
    d = await mock_client["agentos_test"]["agent_messages"].find_one({})
    assert d is not None
    assert d["kind"] == "usage_snapshot"
    assert abs(d["payload"]["total_cost_usd"] - 0.05) < 1e-9


@pytest.mark.asyncio
async def test_policy_decision_archived_with_latency(mock_client):
    sink = MongoMessageSink(client=mock_client, database="agentos_test")
    await sink.emit(
        _ev(
            "policy_decision",
            {
                "engine": "opa",
                "decision": "deny",
                "reason": "tool not allowed",
                "latency_ms": 12.5,
                "tool_name": "Bash",
            },
        )
    )
    d = await mock_client["agentos_test"]["agent_messages"].find_one({})
    assert d is not None
    assert d["kind"] == "policy_decision"
    assert d["payload"]["decision"] == "deny"
    # latency is surfaced to the top level for easy aggregation
    assert d["latencyMs"] == 12.5


@pytest.mark.asyncio
async def test_system_message_archived(mock_client):
    sink = MongoMessageSink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("system_message", {"subtype": "init", "data": {"x": 1}}))
    d = await mock_client["agentos_test"]["agent_messages"].find_one({})
    assert d is not None
    assert d["kind"] == "system_message"


# ── rollup kinds skipped (AgentRegistrySink owns them) ─────────────────────


@pytest.mark.asyncio
async def test_session_started_is_skipped(mock_client):
    sink = MongoMessageSink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("session_started", {"model": "m"}))
    docs = await mock_client["agentos_test"]["agent_messages"].find().to_list(length=10)
    assert docs == []


@pytest.mark.asyncio
async def test_session_ended_is_skipped(mock_client):
    sink = MongoMessageSink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("session_ended", {"is_error": False, "result": "done"}))
    docs = await mock_client["agentos_test"]["agent_messages"].find().to_list(length=10)
    assert docs == []


# ── redaction interplay (middleware runs before sinks; archive sees redacted) ───


@pytest.mark.asyncio
async def test_archive_sees_redacted_payload_via_pipeline(mock_client):
    """A PiiRedactor in front of the sink redacts before the archive write —
    the standard pipeline ordering."""
    sink = MongoMessageSink(client=mock_client, database="agentos_test")
    pipe = Pipeline(middleware=[PiiRedactor(strategy="mask")], sinks=[sink])
    await pipe.emit(_ev("assistant_message", {"text": "email alice@example.com please"}))
    await pipe.flush()

    d = await mock_client["agentos_test"]["agent_messages"].find_one({})
    assert d is not None
    assert "alice@example.com" not in d["payload"]["text"]
    assert "****" in d["payload"]["text"]


# ── truncation ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_long_string_payload_is_truncated(mock_client):
    sink = MongoMessageSink(client=mock_client, database="agentos_test", max_field_chars=20)
    big = "z" * 200
    await sink.emit(_ev("assistant_message", {"text": big}))

    d = await mock_client["agentos_test"]["agent_messages"].find_one({})
    assert d is not None
    text = d["payload"]["text"]
    # Truncated to ~20 chars + footer.
    assert text.startswith("z" * 20)
    assert "+180 chars" in text


@pytest.mark.asyncio
async def test_nested_strings_truncated(mock_client):
    sink = MongoMessageSink(client=mock_client, database="agentos_test", max_field_chars=10)
    await sink.emit(
        _ev(
            "tool_use",
            {
                "tool_name": "Read",
                "tool_input": {"command": "x" * 1000, "args": ["y" * 500]},
            },
        )
    )
    d = await mock_client["agentos_test"]["agent_messages"].find_one({})
    assert d is not None
    assert len(d["payload"]["tool_input"]["command"]) < 100
    assert len(d["payload"]["tool_input"]["args"][0]) < 100


# ── optional extra path (already proven for AgentRegistrySink; same shape) ───


def test_sink_raises_without_mongo_url_and_client():
    with pytest.raises(ValueError, match="requires mongo_url"):
        MongoMessageSink()


def test_sink_uses_collection_param(mock_client):
    sink = MongoMessageSink(client=mock_client, database="agentos_test", collection="custom_msgs")
    # Just verify the collection is bound; emit lands in the right place.
    coll = sink._coll  # type: ignore[attr-defined]
    assert coll.name == "custom_msgs"
