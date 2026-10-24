"""ComputerAgent telemetry goes only to its egress routes.

OTLP to ``telemetry_url`` and the AgentOS dashboard events to
``{events_url}/events``, both with the key on ``x-api-key``. Unset means off,
never a fallback to the ``AGENTOS_*`` destinations.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from computeragent.harness import ComputerAgent
from computeragent.harness.telemetry import build_pipeline
from computeragent.telemetry import TelemetryEvent
from computeragent.telemetry.pipeline import Pipeline
from computeragent.telemetry.sinks.agentos_http import AgentOSHttpSink

AGENT_ID = "6aa93f1b3817a13d3103d2c1"
BASE = "http://localhost:8788/agentos/api"
URL = f"{BASE}/v1/agents/{AGENT_ID}"
KEY = "sbx_key"
EVENTS = "http://egress.internal/ingest"
OTLP = "http://egress.internal/otlp"
AGENT_API = "http://egress.internal/agent-api"


def build(agent_doc, **kwargs) -> ComputerAgent:
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))
    respx.get(f"{AGENT_API}/v3/org/rai-policy").mock(
        return_value=httpx.Response(200, json={"policies": []})
    )
    params: dict = {
        "agent_id": AGENT_ID,
        "base_url": BASE,
        "api_key": KEY,
        "engine": "gitagent",
        "llm_url": "http://egress.internal/llm",
        "mcp_url": "http://egress.internal/mcp",
        "agent_api_url": AGENT_API,
        "srs_url": "http://egress.internal/srs",
    }
    params.update(kwargs)
    return ComputerAgent(**params)


def sinks_of(agent: ComputerAgent) -> list:
    return list(agent._coordinator._pipeline._sinks)  # type: ignore[attr-defined]


def test_no_urls_means_no_sinks():
    assert build_pipeline(telemetry_url=None, events_url=None, api_key=lambda: KEY)._sinks == []


@respx.mock
async def test_events_go_to_events_url_with_the_key_read_per_post():
    route = respx.post(f"{EVENTS}/events").mock(return_value=httpx.Response(200))
    keys = iter(["first", "second"])
    pipeline = build_pipeline(telemetry_url=None, events_url=EVENTS, api_key=lambda: next(keys))
    (sink,) = pipeline._sinks
    assert isinstance(sink, AgentOSHttpSink)

    await sink.emit(TelemetryEvent(kind="user_message", session_id="s", payload={"text": "a"}))
    await sink.flush()
    await sink.emit(TelemetryEvent(kind="user_message", session_id="s", payload={"text": "b"}))
    await sink.aclose()

    assert [c.request.headers["x-api-key"] for c in route.calls] == ["first", "second"]
    assert all("authorization" not in c.request.headers for c in route.calls)
    assert json.loads(route.calls[0].request.content)


def test_telemetry_url_builds_an_otlp_sink_with_the_key():
    otel = pytest.importorskip("computeragent.telemetry.sinks.otel")

    (sink,) = build_pipeline(telemetry_url=OTLP, events_url=None, api_key=lambda: KEY)._sinks

    assert isinstance(sink, otel.OtelSink)
    assert sink._endpoint == OTLP  # type: ignore[attr-defined]
    assert sink._headers == {"x-api-key": KEY}  # type: ignore[attr-defined]
    assert sink._exporter == "otlp-http"  # type: ignore[attr-defined]


@respx.mock
def test_agentos_env_destinations_are_ignored(agent_doc, monkeypatch):
    monkeypatch.setenv("AGENTOS_INGEST_URL", "http://agentos.direct/ingest/events")
    monkeypatch.delenv("COMPUTERAGENT_OTEL", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://vendor.direct")

    agent = build(agent_doc)

    assert sinks_of(agent) == []


@respx.mock
def test_events_url_reaches_the_coordinators_pipeline(agent_doc):
    agent = build(agent_doc, events_url=EVENTS)

    (sink,) = sinks_of(agent)
    assert isinstance(sink, AgentOSHttpSink)
    assert sink._url == f"{EVENTS}/events"  # type: ignore[attr-defined]


@respx.mock
def test_a_callers_pipeline_wins(agent_doc):
    mine = Pipeline(sinks=[])

    agent = build(agent_doc, telemetry_pipeline=mine, events_url=EVENTS)

    assert agent._coordinator._pipeline is mine  # type: ignore[attr-defined]
    assert agent._owned_pipeline is None


@respx.mock
async def test_dispose_closes_the_pipeline_it_built(agent_doc):
    agent = build(agent_doc, events_url=EVENTS)
    (sink,) = sinks_of(agent)
    closed = []
    original = sink.aclose

    async def tracking_aclose():
        closed.append(True)
        await original()

    sink.aclose = tracking_aclose  # type: ignore[method-assign]

    await agent.dispose()

    assert closed == [True]
