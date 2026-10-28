"""AgentCore's front door: /ping and relay envelope v1 on /invocations.

Cases mirror computeragent-enterprise/packages/runtime-agentcore/src/relay.test.ts.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from types import SimpleNamespace

import pytest
import respx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from computeragent.server.relay import (
    RELAY_STREAM_HEADER,
    encode_body,
    parse_relay_request,
    relay_routes,
)
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


# ── stream close, size cap, logging (a minimal app behind relay_routes) ─────


def relay_app(**options):
    """A tiny app behind the relay: /headers echoes request headers, /events
    streams until closed, /blob?n=N answers N bytes."""
    state = {"seen": [], "opened": asyncio.Event(), "open": False}

    async def headers(request):
        state["seen"].append(dict(request.headers))
        return JSONResponse({"ok": True})

    async def events(_):
        async def gen():
            state["open"] = True
            state["opened"].set()
            try:
                yield b"id: 1\nevent: hello\ndata: {}\n\n"
                await asyncio.Event().wait()
            finally:
                state["open"] = False

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def blob(request):
        return Response(b"a" * int(request.query_params["n"]), media_type="text/plain")

    app = None
    ping, invocations = relay_routes(
        lambda: app, SimpleNamespace(running=0, last_update=0), **options
    )
    app = Starlette(
        routes=[
            Route("/headers", headers, methods=["GET", "POST"]),
            Route("/events", events),
            Route("/blob", blob),
            Route("/ping", ping),
            Route("/invocations", invocations, methods=["POST"]),
        ]
    )
    return app, state


def close_env(sid):
    return envelope("DELETE", f"/_relay/streams/{sid}")


async def test_delete_closes_an_open_stream(caplog):
    caplog.set_level(logging.INFO, logger="computeragent.server")
    app, state = relay_app()
    sid = str(uuid.uuid4())
    async with client_for(app) as client:
        sse = asyncio.create_task(
            invoke(client, envelope("GET", "/events", headers={"X-CA-Relay-Stream": sid}))
        )
        await asyncio.wait_for(state["opened"].wait(), 5)
        closed = (await invoke(client, close_env(sid))).json()
        streamed = await asyncio.wait_for(sse, 5)
        again = (await invoke(client, close_env(sid))).json()

    assert closed["status"] == 204 and closed["instance"] and "body" not in closed
    assert state["open"] is False
    head, rest = streamed.text.split("\n\n", 1)
    assert json.loads(head[len(": ca-relay ") :])["status"] == 200
    assert frames(rest) == [(1, "hello", {})]
    assert again["status"] == 404 and "body" not in again
    logged = [r.getMessage() for r in caplog.records]
    assert logged.count(f"relay.stream.opened stream={sid}") == 1
    assert logged.count(f"relay.stream.closed stream={sid} reason=deleted") == 1
    assert not [m for m in logged if "reason=ended" in m]


@pytest.mark.parametrize("target", [str(uuid.uuid4()), "not-a-uuid", "../x"])
async def test_close_of_an_unknown_stream_is_a_404(target):
    app, state = relay_app()
    async with client_for(app) as client:
        got = (await invoke(client, close_env(target))).json()

    assert got["status"] == 404 and "body" not in got


async def test_other_methods_on_the_close_path_reach_the_app():
    app, _ = relay_app()
    async with client_for(app) as client:
        got = (await invoke(client, envelope("GET", f"/_relay/streams/{uuid.uuid4()}"))).json()

    assert got["status"] == 404  # the app's own answer: it has no such route


@pytest.mark.parametrize("name", ["x-ca-relay-stream", "X-CA-Relay-Stream", "X-CA-RELAY-STREAM"])
@pytest.mark.parametrize("value", [str(uuid.uuid4()), "not-a-uuid"])
async def test_the_app_never_sees_the_stream_header(name, value):
    app, state = relay_app()
    async with client_for(app) as client:
        got = (await invoke(client, envelope("GET", "/headers", headers={name: value}))).json()

    assert got["status"] == 200
    (seen,) = state["seen"]
    assert "x-ca-relay-stream" not in seen
    assert value not in seen.values()
    assert seen["content-type"] == "application/json"


async def test_an_envelope_over_the_cap_is_a_502():
    blob = envelope("GET", "/blob?n=1000")
    app, _ = relay_app()
    async with client_for(app) as client:
        size = len((await invoke(client, blob)).content)
    app, _ = relay_app(max_response_bytes=size)
    async with client_for(app) as client:
        at_cap = await invoke(client, blob)
    app, _ = relay_app(max_response_bytes=size - 1)
    async with client_for(app) as client:
        over = (await invoke(client, blob)).json()

    assert len(at_cap.content) == size
    assert at_cap.json()["status"] == 200 and at_cap.json()["body"] == "a" * 1000
    assert over["relay"] == 1 and over["instance"]
    assert over["status"] == 502
    assert over["headers"] == {"content-type": "application/json"}
    assert json.loads(over["body"]) == {
        "detail": "The response is larger than AgentCore can carry.",
        "code": "AGENTCORE_RESPONSE_TOO_LARGE",
        "retry": False,
        "severity": "error",
        "source": "CAS_RUNTIME",
        "meta": {"maxBytes": size - 1},
    }


async def test_relay_logs_ids_only(caplog):
    caplog.set_level(logging.INFO, logger="computeragent.server")
    app, _ = relay_app()
    sids = [str(uuid.uuid4()), str(uuid.uuid4())]
    secret = {"authorization": "Bearer sbt_secret", "x-other": "hdr-secret"}
    async with client_for(app) as client:
        instance = (await invoke(client, envelope("GET", "/headers"))).json()["instance"]
        for sid in sids:
            env = envelope(
                "POST",
                "/headers",
                {"q": "body-secret"},
                headers={**secret, RELAY_STREAM_HEADER: sid},
            )
            await invoke(client, env)

    logged = [r.getMessage() for r in caplog.records]
    assert logged.count(f"relay.instance id={instance}") == 1
    assert len([m for m in logged if m.startswith("relay.instance")]) == 1
    for sid in sids:
        assert logged.count(f"relay.stream.opened stream={sid}") == 1
        assert logged.count(f"relay.stream.closed stream={sid} reason=ended") == 1
    text = " ".join(logged)
    for leak in ("sbt_secret", "hdr-secret", "body-secret", '"ok"'):
        assert leak not in text
