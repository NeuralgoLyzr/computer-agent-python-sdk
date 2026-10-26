"""The Claude CLI's loopback forwarder to the egress proxy."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
import pytest

from computeragent.harness.egress_forwarder import EgressForwarder
from computeragent.harness.egress_token import (
    CREDENTIAL,
    HEADER_EXPIRES_AT,
    HEADER_TOKEN,
    egress_event_hooks,
)

EGRESS = "http://egress.test"


def later(hours: float = 4) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def forwarder_with(handler) -> EgressForwarder:
    CREDENTIAL.configure([f"{EGRESS}/llm"])
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), event_hooks=egress_event_hooks(True)
    )
    return EgressForwarder(client=upstream)


async def test_forwards_and_stamps():
    CREDENTIAL.configure([f"{EGRESS}/llm"])
    CREDENTIAL.adopt("sbt_a", "sbt_b", later())
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        await request.aread()
        seen.append(request)
        return httpx.Response(201, headers={"x-upstream": "yes"}, content=b"done")

    fwd = forwarder_with(handler)
    try:
        local = await fwd.url_for(f"{EGRESS}/llm/v1/messages?beta=true")
        assert urlsplit(local).hostname == "127.0.0.1"
        assert urlsplit(local).path == "/llm/v1/messages"
        async with httpx.AsyncClient() as cli:
            resp = await cli.post(
                local,
                content=b'{"x":1}',
                headers={
                    "x-api-key": "sbt_a",
                    "anthropic-version": "2023-06-01",
                    "connection": "keep-alive",
                    "proxy-authorization": "Basic zzz",
                },
            )
    finally:
        await fwd.aclose()

    assert resp.status_code == 201
    assert resp.content == b"done"
    assert resp.headers["x-upstream"] == "yes"
    (req,) = seen
    assert req.method == "POST"
    assert str(req.url) == f"{EGRESS}/llm/v1/messages?beta=true"
    assert req.content == b'{"x":1}'
    assert req.headers["x-api-key"] == "sbt_b"
    assert req.headers["anthropic-version"] == "2023-06-01"
    assert "proxy-authorization" not in req.headers


async def test_streams_both_ways():
    release = asyncio.Event()
    received: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        async for chunk in request.stream:
            received.append(chunk)

        async def sse():
            yield b"data: one\n\n"
            await release.wait()
            yield b"data: two\n\n"

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse())

    async def upload():
        yield b"part-1;"
        yield b"part-2"

    fwd = forwarder_with(handler)
    try:
        local = await fwd.url_for(f"{EGRESS}/llm/v1/messages")
        async with (
            httpx.AsyncClient() as cli,
            cli.stream("POST", local, content=upload()) as resp,
        ):
            chunks = resp.aiter_raw()
            first = await asyncio.wait_for(chunks.__anext__(), timeout=5)
            assert b"data: one" in first
            release.set()
            rest = b"".join([c async for c in chunks])
    finally:
        await fwd.aclose()

    assert b"data: two" in rest
    assert b"".join(received) == b"part-1;part-2"


async def test_reissue_seen_through_forwarder():
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["x-api-key"])
        if len(calls) == 1:
            return httpx.Response(
                200, headers={HEADER_TOKEN: "sbt_new", HEADER_EXPIRES_AT: later()}
            )
        return httpx.Response(200)

    fwd = forwarder_with(handler)
    try:
        local = await fwd.url_for(f"{EGRESS}/mcp/v1/jira")
        async with httpx.AsyncClient() as cli:
            await cli.get(local, headers={"x-api-key": "sbt_old"})
            await cli.get(local, headers={"x-api-key": "sbt_old"})
    finally:
        await fwd.aclose()

    assert calls == ["sbt_old", "sbt_new"]
    assert CREDENTIAL.resolve("sbt_old") == "sbt_new"


async def test_loopback_and_errors():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream detail that must not leak")

    fwd = forwarder_with(handler)
    try:
        local = await fwd.url_for(f"{EGRESS}/llm/v1/messages")
        assert urlsplit(local).hostname == "127.0.0.1"
        async with httpx.AsyncClient() as cli:
            resp = await cli.post(local, content=b"x")
    finally:
        await fwd.aclose()

    assert resp.status_code == 502
    assert resp.content == b""


async def test_a_non_egress_url_is_left_alone():
    CREDENTIAL.configure([f"{EGRESS}/llm"])
    fwd = EgressForwarder()
    try:
        assert await fwd.url_for("http://elsewhere.test/mcp") == "http://elsewhere.test/mcp"
    finally:
        await fwd.aclose()


async def test_cli_urls_rewritten():
    CREDENTIAL.configure([f"{EGRESS}/llm"])
    fwd = EgressForwarder()
    gap_tools = object()
    try:
        envs, options = await fwd.rewrite(
            {"ANTHROPIC_BASE_URL": f"{EGRESS}/llm", "OTHER": "x"},
            {
                "mcp_servers": {
                    "jira": {"type": "http", "url": f"{EGRESS}/mcp/v1/jira", "headers": {}},
                    "mine": {"type": "http", "url": "http://elsewhere.test/mcp"},
                    "gap_tools": gap_tools,
                },
                "model": "m",
            },
        )
    finally:
        await fwd.aclose()

    base = urlsplit(envs["ANTHROPIC_BASE_URL"])
    assert (base.hostname, base.path) == ("127.0.0.1", "/llm")
    assert envs["OTHER"] == "x"
    jira = urlsplit(options["mcp_servers"]["jira"]["url"])
    assert (jira.hostname, jira.path) == ("127.0.0.1", "/mcp/v1/jira")
    assert jira.port == base.port  # one listener per egress origin
    assert options["mcp_servers"]["mine"]["url"] == "http://elsewhere.test/mcp"
    assert options["mcp_servers"]["gap_tools"] is gap_tools
    assert options["model"] == "m"


@pytest.mark.parametrize("engine_name", ["gitagent"])
async def test_gitagent_is_not_forwarded(engine_name, agent_doc):
    """Only the Claude CLI goes through the forwarder; gitagent's clients are
    hooked in-process."""
    import respx

    from computeragent.harness import ComputerAgent

    with respx.mock:
        respx.get(f"{EGRESS}/agentos/api/v1/agents/6aa93f1b3817a13d3103d2c1").mock(
            return_value=httpx.Response(200, json=agent_doc)
        )
        agent = ComputerAgent(
            agent_id="6aa93f1b3817a13d3103d2c1",
            base_url=f"{EGRESS}/agentos/api",
            api_key="sbt_a",
            engine=engine_name,
            llm_url=f"{EGRESS}/llm",
            mcp_url=f"{EGRESS}/mcp",
            guardrails_url=f"{EGRESS}/guardrails",
        )
    assert agent._coordinator._forward_cli is False  # type: ignore[attr-defined]
