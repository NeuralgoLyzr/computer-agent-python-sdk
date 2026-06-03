"""``AgentRegistrySink`` — Mongo doc shape parity with TS @open-gitagent/agent-registry-mongo."""

from __future__ import annotations

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from computeragent.telemetry import TelemetryEvent  # noqa: E402
from computeragent.telemetry.sinks import AgentRegistrySink  # noqa: E402


def _ev(kind, payload=None, sid="sess_a", agent_name="AcmeAssist"):
    return TelemetryEvent(kind=kind, session_id=sid, agent_name=agent_name, payload=payload or {})


@pytest.fixture
def mock_client():
    return AsyncMongoMockClient()


@pytest.mark.asyncio
async def test_session_started_inserts_agent_registry_with_registered_at(mock_client):
    sink = AgentRegistrySink(client=mock_client, database="agentos_test", registered_by="pod-1")
    await sink.emit(_ev("session_started", {"model": "claude-sonnet-4-6", "prompt": "q"}))

    docs = await mock_client["agentos_test"]["agent_registry"].find().to_list(length=10)
    assert len(docs) == 1
    d = docs[0]
    assert d["_id"] == "AcmeAssist"
    assert d["harness"] == "claude-agent-sdk"
    assert d["model"] == "claude-sonnet-4-6"
    assert d["registeredBy"] == "pod-1"
    assert "registeredAt" in d
    assert d["registeredAt"] == d["updatedAt"]
    assert d["registeredAt"] == d["lastSeen"]


@pytest.mark.asyncio
async def test_session_started_writes_inline_source_with_files_for_live_chat(mock_client):
    """The registry doc's `source` field must include a `files` dict with
    `agent.yaml` + `CLAUDE.md` so the AgentOS SPA's chat-sandbox flow can
    spin up a live sandbox via the harness's inline identity loader."""
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(
        _ev(
            "session_started",
            {
                "model": "claude-sonnet-4-5",
                "prompt": "irrelevant",
                "system_prompt": "You are AcmeAssist. Answer concisely.",
                "allowed_tools": ["Read", "Glob", "Grep"],
                "permission_mode": "bypassPermissions",
                "max_turns": 12,
            },
        )
    )
    d = await mock_client["agentos_test"]["agent_registry"].find_one({"_id": "AcmeAssist"})
    assert d is not None
    src = d["source"]
    assert src["type"] == "inline"
    assert src["manifest"]["name"] == "AcmeAssist"
    files = src["files"]
    # CLAUDE.md carries the system_prompt verbatim.
    assert files["CLAUDE.md"] == "You are AcmeAssist. Answer concisely."
    # agent.yaml encodes model + allowed_tools + max_turns + permission_mode.
    yaml = files["agent.yaml"]
    assert "claude-sonnet-4-5" in yaml
    assert "Read" in yaml and "Glob" in yaml and "Grep" in yaml
    assert "max_turns: 12" in yaml
    assert "bypassPermissions" in yaml


@pytest.mark.asyncio
async def test_inline_source_falls_back_to_defaults_when_options_missing(mock_client):
    """If the user didn't pass system_prompt / allowed_tools, the inline
    manifest still ships with sane defaults so chat-sandbox can succeed."""
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("session_started", {"model": "claude-sonnet-4-5", "prompt": "x"}))
    d = await mock_client["agentos_test"]["agent_registry"].find_one({"_id": "AcmeAssist"})
    assert d is not None
    files = d["source"]["files"]
    # CLAUDE.md gets a default helpful-assistant instruction.
    assert "helpful assistant" in files["CLAUDE.md"].lower()
    # agent.yaml defaults to Read/Glob/Grep tools.
    yaml = files["agent.yaml"]
    assert "Read" in yaml and "Glob" in yaml and "Grep" in yaml


@pytest.mark.asyncio
async def test_second_session_started_keeps_registered_at_but_updates_last_seen(mock_client):
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("session_started", {"model": "m"}))
    doc1 = await mock_client["agentos_test"]["agent_registry"].find_one({"_id": "AcmeAssist"})

    # Different session id but same agent name — second upsert.
    await sink.emit(_ev("session_started", {"model": "m"}, sid="sess_b"))
    doc2 = await mock_client["agentos_test"]["agent_registry"].find_one({"_id": "AcmeAssist"})

    assert doc1["registeredAt"] == doc2["registeredAt"]
    # updatedAt and lastSeen are refreshed on subsequent upserts (mongomock
    # may use the same clock tick — assert at least non-decreasing).
    assert doc2["updatedAt"] >= doc1["updatedAt"]
    assert doc2["lastSeen"] >= doc1["lastSeen"]


@pytest.mark.asyncio
async def test_session_ended_inserts_agent_log(mock_client):
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("session_started", {"prompt": "How does routing work?", "model": "m"}))
    await sink.emit(
        _ev(
            "session_ended",
            {
                "is_error": False,
                "result": "Routing uses BM25.",
                "num_turns": 3,
                "total_cost_usd": 0.012,
                "duration_ms": 800.0,
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )
    )

    logs = await mock_client["agentos_test"]["agent_logs"].find().to_list(length=10)
    assert len(logs) == 1
    log = logs[0]
    assert log["_id"].startswith("log_")
    assert log["source"] == "library"
    assert log["agentName"] == "AcmeAssist"
    # TS agentos-server counts per-agent logs by the `bot` field
    # (a holdover from Slack-bot logs). We write it alongside agentName
    # so the existing /api/agents logCount aggregation lights up.
    assert log["bot"] == "AcmeAssist"
    assert log["sessionId"] == "sess_a"
    assert log["query"] == "How does routing work?"
    assert log["reply"] == "Routing uses BM25."
    assert log["ok"] is True
    assert log["durationMs"] == 800
    assert log["inputTokens"] == 100
    assert log["outputTokens"] == 50
    assert abs(log["costUsd"] - 0.012) < 1e-9


@pytest.mark.asyncio
async def test_truncation_matches_ts_constants(mock_client):
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    long_query = "x" * 12_000  # > QUERY_MAX=8000
    long_reply = "y" * 25_000  # > REPLY_MAX=16000
    await sink.emit(_ev("session_started", {"prompt": long_query, "model": "m"}))
    await sink.emit(
        _ev(
            "session_ended",
            {
                "is_error": False,
                "result": long_reply,
                "num_turns": 1,
                "total_cost_usd": 0.001,
                "duration_ms": 10.0,
                "usage": {},
            },
        )
    )

    log = await mock_client["agentos_test"]["agent_logs"].find_one({})
    assert log is not None
    assert len(log["query"]) == 8_000
    assert len(log["reply"]) == 16_000


@pytest.mark.asyncio
async def test_error_session_writes_ok_false_with_message(mock_client):
    sink = AgentRegistrySink(client=mock_client, database="agentos_test")
    await sink.emit(_ev("session_started", {"prompt": "q", "model": "m"}))
    await sink.emit(
        _ev(
            "session_ended",
            {
                "is_error": True,
                "subtype": "error",
                "error_type": "RuntimeError",
                "error_message": "boom",
                "duration_ms": 5.0,
                "usage": {},
                "result": "",
                "num_turns": 0,
                "total_cost_usd": 0.0,
            },
        )
    )

    log = await mock_client["agentos_test"]["agent_logs"].find_one({})
    assert log is not None
    assert log["ok"] is False
    assert log["error"] == "boom"
