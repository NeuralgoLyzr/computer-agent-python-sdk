"""AgentCore's front door: /ping and relay envelope v1 on /invocations.

Cases mirror computeragent-enterprise/packages/runtime-agentcore/src/relay.test.ts.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
import respx

from computeragent.server.relay import encode_body, parse_relay_request
from server_helpers import FakeEngine, client_for, frames, make_app, mock_backends, session_body


def envelope(method, path, body=None, *, raw=None, headers=None):
    env = {
        "relay": 1,
        "method": method,
        "path": path,
        "headers": {"content-type": "application/json", **(headers or {})},
    }
    if body is not None:
        env["body"] = json.dumps(body)
    if raw is not None:
        env.update(encode_body(raw))
    return env


async def invoke(client, env):
    r = await client.post("/invocations", content=json.dumps(env).encode())
    assert r.status_code == 200
    return r


@respx.mock
async def test_ping(agent_doc):
    mock_backends(agent_doc)
    hold = asyncio.Event()
    async with client_for(make_app(FakeEngine(hold=hold))) as client:
        idle = (await client.get("/ping")).json()
        await client.post("/v1/sessions", json=session_body())
        await client.post(
            "/v1/sessions/s1/messages", json={"message": {"role": "user", "content": "x"}}
        )
        for _ in range(100):
            busy = (await client.get("/ping")).json()
            if busy["status"] == "HealthyBusy":
                break
            await asyncio.sleep(0.02)
        hold.set()
        await client.post("/v1/sessions/s1/end-input")
        await client.get("/v1/sessions/s1/events")
        after = (await client.get("/ping")).json()

    assert idle["status"] == "Healthy" and isinstance(idle["time_of_last_update"], int)
    assert busy["status"] == "HealthyBusy"
    assert after["status"] == "Healthy"


@respx.mock
async def test_json_round_trip(agent_doc):
    mock_backends(agent_doc)
    async with client_for(make_app(FakeEngine())) as client:
        created = (await invoke(client, envelope("POST", "/v1/sessions", session_body()))).json()
        missing = (await invoke(client, envelope("GET", "/v1/sessions/nope"))).json()

    assert created["relay"] == 1 and created["status"] == 201 and created["instance"]
    assert created["headers"]["content-type"] == "application/json"
    assert "content-length" not in created["headers"]
    assert json.loads(created["body"])["sessionId"] == "s1"
    assert missing["status"] == 404
    assert json.loads(missing["body"])["code"] == "SESSION_NOT_FOUND"


@respx.mock
async def test_sse_head_frame(agent_doc):
    mock_backends(agent_doc)
    async with client_for(make_app(FakeEngine())) as client:
        await client.post("/v1/sessions", json=session_body())
        await client.post(
            "/v1/sessions/s1/messages", json={"message": {"role": "user", "content": "hi"}}
        )
        await client.post("/v1/sessions/s1/end-input")
        direct = frames((await client.get("/v1/sessions/s1/events")).text)
        relayed = await invoke(
            client,
            envelope(
                "GET",
                "/v1/sessions/s1/events?lastEventId=-1",
                headers={"accept": "text/event-stream"},
            ),
        )

    assert relayed.headers["content-type"].startswith("text/event-stream")
    head, rest = relayed.text.split("\n\n", 1)
    meta = json.loads(head[len(": ca-relay ") :])
    assert meta["status"] == 200 and meta["instance"]
    assert frames(rest) == direct


@respx.mock
async def test_binary_bodies(agent_doc, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock_backends(agent_doc)
    data = bytes([0xFF, 0x00, 0xFE, 0x80])
    async with client_for(make_app(FakeEngine())) as client:
        await client.post("/v1/sessions", json=session_body())
        put = (
            await invoke(client, envelope("PUT", "/v1/sessions/s1/fs/file?path=bin.dat", raw=data))
        ).json()
        got = (await invoke(client, envelope("GET", "/v1/sessions/s1/fs/file?path=bin.dat"))).json()

    assert put["status"] == 200
    assert (tmp_path / "bin.dat").read_bytes() == data
    assert base64.b64decode(got["bodyBase64"]) == data
    assert "body" not in got


@pytest.mark.parametrize(
    "raw",
    [
        {"method": "GET", "path": "/v1/health", "headers": {}},
        {"relay": 2, "method": "GET", "path": "/v1/health", "headers": {}},
        {"relay": 1, "method": "GET", "headers": {}},
        {"relay": 1, "method": "get", "path": "/v1/health", "headers": {}},
        {"relay": 1, "method": "GET", "path": "/v1/health", "headers": {"a": 1}},
        {"relay": 1, "method": "POST", "path": "/v1/sessions", "headers": {}, "bodyBase64": "!!!"},
        {"relay": 1, "method": "POST", "path": "/invocations", "headers": {}},
        "not an envelope",
    ],
)
async def test_malformed_envelope(raw):
    async with client_for(make_app(FakeEngine())) as client:
        r = await client.post("/invocations", content=json.dumps(raw).encode())

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == 400
    assert json.loads(body["body"])["code"] == "AGENTCORE_RELAY_REQUEST_INVALID"


async def test_instance_is_stable_per_process():
    async with client_for(make_app(FakeEngine())) as client:
        first = (await invoke(client, envelope("GET", "/v1/health"))).json()["instance"]
        second = (await invoke(client, envelope("GET", "/v1/health"))).json()["instance"]
    async with client_for(make_app(FakeEngine())) as client:
        other = (await invoke(client, envelope("GET", "/v1/health"))).json()["instance"]

    assert first == second
    assert other != first


def test_parse_relay_request_and_bodies():
    ok = {
        "relay": 1,
        "method": "POST",
        "path": "/v1/sessions/s1/messages?x=y",
        "headers": {"content-type": "application/json"},
        "body": "{}",
    }
    assert parse_relay_request(ok) == (
        "POST",
        "/v1/sessions/s1/messages?x=y",
        {"content-type": "application/json"},
        b"{}",
    )
    assert encode_body('{"hello":"wörld"}'.encode()) == {"body": '{"hello":"wörld"}'}
    assert encode_body(bytes([0xFF, 0x00])) == {
        "bodyBase64": base64.b64encode(bytes([0xFF, 0x00])).decode()
    }
    assert encode_body(b"") == {}
