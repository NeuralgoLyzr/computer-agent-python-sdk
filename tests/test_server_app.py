"""The app server's routes: hooks, health, sessions, messages and the per-turn key."""

from __future__ import annotations

import logging

import pytest
import respx

from computeragent.server.app import default_agent_factory
from server_helpers import (
    FakeEngine,
    client_for,
    env_key,
    frames,
    make_app,
    mock_backends,
    session_body,
)


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("LYZR_API_KEY", raising=False)
    monkeypatch.delenv("OTHER", raising=False)


@pytest.mark.parametrize("hook", ["ready", "validate", "run", "suspend", "resume", "terminate"])
async def test_lifecycle_hooks_and_health(hook):
    async with client_for(make_app(FakeEngine())) as client:
        assert (await client.post(f"/aws/lambda-microvms/runtime/v1/{hook}")).status_code == 200
        health = await client.get("/v1/health")

    assert health.status_code == 200
    body = health.json()
    assert body["ok"] is True
    assert {"claude-agent-sdk", "gitagent"} <= set(body["engines"])
    assert set(body["engines"]["claude-agent-sdk"]) == {
        "streamingInput",
        "partialMessages",
        "permissionCallback",
        "sessions",
        "budget",
    }


@respx.mock
async def test_create_session_seeds_the_key(agent_doc):
    mock_backends(agent_doc)
    calls: list = []
    async with client_for(make_app(FakeEngine(), calls)) as client:
        r = await client.post("/v1/sessions", json=session_body())

    assert r.status_code == 201
    body = r.json()
    assert body["sessionId"] == "s1"
    assert body["eventsUrl"] == "/v1/sessions/s1/events"
    assert body["identity"]["name"] == agent_doc["name"]
    assert env_key() == "sbt_1"
    import os

    assert "OTHER" not in os.environ
    assert "api_key" not in calls[0]
    assert calls[0]["session_id"] == "s1"


def test_the_default_factory_passes_no_api_key(monkeypatch):
    seen = {}

    class Recorder:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr("computeragent.harness.ComputerAgent", Recorder)
    monkeypatch.setenv("COMPUTERAGENT_ENGINE", "gitagent")

    default_agent_factory(session_id="s", options=None, session_store=None, on_tool_call=print)

    assert "api_key" not in seen
    assert seen["on_tool_call"] is print


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (session_body(engine="langgraph"), "ENGINE_NAME_UNKNOWN"),
        (session_body(sessionId="../x"), "SESSION_ID_INVALID"),
        (session_body(engine=""), "REQUEST_BODY_INVALID"),
        (session_body(messages=[{"role": "assistant", "content": "x"}]), "REQUEST_BODY_INVALID"),
    ],
)
async def test_create_session_refusals(body, code):
    async with client_for(make_app(FakeEngine())) as client:
        r = await client.post("/v1/sessions", json=body)

    assert r.status_code == 400
    assert r.json()["code"] == code


@respx.mock
async def test_message_credential_is_the_turn_key(agent_doc):
    mock_backends(agent_doc)
    engine = FakeEngine()
    async with client_for(make_app(engine)) as client:
        await client.post("/v1/sessions", json=session_body())
        r = await client.post(
            "/v1/sessions/s1/messages",
            json={"message": {"role": "user", "content": "one"}, "credential": "sbt_2"},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        await client.post(
            "/v1/sessions/s1/messages", json={"message": {"role": "user", "content": "two"}}
        )
        await client.post("/v1/sessions/s1/end-input")
        await client.get("/v1/sessions/s1/events")

    keys = [ctx.envs["LYZR_API_KEY"] for ctx in engine.contexts]
    assert keys == ["sbt_2", "sbt_2"]  # without a credential the previous key stays


@respx.mock
async def test_session_routes(agent_doc):
    mock_backends(agent_doc)
    async with client_for(make_app(FakeEngine())) as client:
        await client.post("/v1/sessions", json=session_body())
        status = await client.get("/v1/sessions/s1")
        assert status.json()["status"] in ("pending", "running")
        assert (await client.post("/v1/sessions/s1/cancel")).json() == {"ok": True}
        events = frames((await client.get("/v1/sessions/s1/events")).text)
        assert events[-1][2] == {
            "kind": "ca_session_ended",
            "sessionId": "s1",
            "reason": "cancelled",
        }
        assert (await client.get("/v1/sessions/s1")).json()["status"] == "cancelled"
        r = await client.post(
            "/v1/sessions/s1/messages", json={"message": {"role": "user", "content": "x"}}
        )
        assert r.status_code == 409
        assert (await client.delete("/v1/sessions/s1")).json() == {"ok": True}
        for method, path in [
            ("GET", "/v1/sessions/s1"),
            ("DELETE", "/v1/sessions/s1"),
            ("POST", "/v1/sessions/s1/end-input"),
            ("POST", "/v1/sessions/s1/cancel"),
            ("GET", "/v1/sessions/s1/events"),
            ("POST", "/v1/sessions/s1/permission/c1"),
        ]:
            r = await client.request(method, path, json={"decision": "allow"})
            assert r.status_code == 404, path
            assert r.json()["code"] == "SESSION_NOT_FOUND"


@respx.mock
async def test_end_input_completes_the_session(agent_doc):
    mock_backends(agent_doc)
    async with client_for(make_app(FakeEngine())) as client:
        await client.post(
            "/v1/sessions",
            json=session_body(
                messages=[{"role": "user", "content": "first"}], streamingInput=False
            ),
        )
        events = frames((await client.get("/v1/sessions/s1/events")).text)

    kinds = [e[1] for e in events]
    assert kinds[0] == "ca_session_started"
    assert "ca_turn_started" in kinds
    assert events[-1][2]["reason"] == "complete"


@respx.mock
async def test_never_logs_secrets(agent_doc, caplog):
    mock_backends(agent_doc)
    with caplog.at_level(logging.DEBUG):
        async with client_for(make_app(FakeEngine())) as client:
            await client.post(
                "/v1/sessions", json=session_body(envs={"LYZR_API_KEY": "sbt_secret_1"})
            )
            await client.post(
                "/v1/sessions/s1/messages",
                json={"message": {"role": "user", "content": "hi"}, "credential": "sbt_secret_2"},
            )
            await client.post("/v1/sessions/s1/end-input")
            await client.get("/v1/sessions/s1/events")

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "sbt_secret" not in logged
