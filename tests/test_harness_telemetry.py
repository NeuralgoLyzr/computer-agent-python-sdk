"""ComputerAgent telemetry goes only to its egress route.

OTLP to ``telemetry_url`` with the key on ``x-api-key``. Unset means off, never
a fallback to the ``AGENTOS_*`` destinations.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from computeragent.harness import ComputerAgent
from computeragent.harness.telemetry import build_pipeline
from computeragent.telemetry.pipeline import Pipeline

AGENT_ID = "6aa93f1b3817a13d3103d2c1"
BASE = "http://localhost:8788/agentos/api"
URL = f"{BASE}/v1/agents/{AGENT_ID}"
KEY = "sbx_key"
OTLP = "http://egress.internal/otlp"
GUARDRAILS = "http://egress.internal/guardrails"
CHECK_URL = f"{GUARDRAILS}/v1/check"


def build(agent_doc, **kwargs) -> ComputerAgent:
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))
    respx.post("http://egress.internal/guardrails/v1/check").mock(
        return_value=httpx.Response(200, json={"data": {"decision": "allow"}})
    )
    params: dict = {
        "agent_id": AGENT_ID,
        "base_url": BASE,
        "api_key": KEY,
        "engine": "gitagent",
        "llm_url": "http://egress.internal/llm",
        "mcp_url": "http://egress.internal/mcp",
        "guardrails_url": "http://egress.internal/guardrails",
    }
    params.update(kwargs)
    return ComputerAgent(**params)


def sinks_of(agent: ComputerAgent) -> list:
    return list(agent._coordinator._pipeline._sinks)  # type: ignore[attr-defined]


def test_no_urls_means_no_sinks():
    assert build_pipeline(telemetry_url=None, api_key=lambda: KEY)._sinks == []


@respx.mock
def test_no_events_sink(agent_doc):
    """The AgentOS dashboard events sink is gone: no events_url anywhere."""
    with pytest.raises(TypeError, match="events_url"):
        build_pipeline(telemetry_url=None, events_url="http://x", api_key=lambda: KEY)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="events_url"):
        build(agent_doc, events_url="http://egress.internal/ingest")

    from computeragent.telemetry import sinks

    assert not hasattr(sinks, "AgentOSHttpSink")


def test_telemetry_url_builds_an_otlp_sink_with_the_key():
    otel = pytest.importorskip("computeragent.telemetry.sinks.otel")

    (sink,) = build_pipeline(telemetry_url=OTLP, api_key=lambda: KEY)._sinks

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
def test_telemetry_url_reaches_the_coordinators_pipeline(agent_doc):
    otel = pytest.importorskip("computeragent.telemetry.sinks.otel")

    agent = build(agent_doc, telemetry_url=OTLP)

    (sink,) = sinks_of(agent)
    assert isinstance(sink, otel.OtelSink)


@respx.mock
def test_a_callers_pipeline_wins(agent_doc):
    mine = Pipeline(sinks=[])

    agent = build(agent_doc, telemetry_pipeline=mine, telemetry_url=OTLP)

    assert agent._coordinator._pipeline is mine  # type: ignore[attr-defined]
    assert agent._owned_pipeline is None


@respx.mock
async def test_dispose_closes_the_pipeline_it_built(agent_doc):
    pytest.importorskip("computeragent.telemetry.sinks.otel")
    agent = build(agent_doc, telemetry_url=OTLP)
    (sink,) = sinks_of(agent)
    closed = []
    original = sink.aclose

    async def tracking_aclose():
        closed.append(True)
        await original()

    sink.aclose = tracking_aclose  # type: ignore[method-assign]

    await agent.dispose()

    assert closed == [True]
