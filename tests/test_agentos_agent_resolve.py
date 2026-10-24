"""Resolving an agent's run definition from AgentOS.

Covers the contract of ``GET /v1/agents/:id`` as this SDK consumes it: the
credential header, model precedence, which agent shapes are refused, and the
error text for each HTTP failure — the last because a 401 on a valid-looking
key and a 404 on a real agent both have one likely cause each, and saying which
is the difference between a two-minute fix and an afternoon.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from computeragent.harness.agentos_agent import (
    AgentResolveError,
    ResolvedAgent,
    UnsupportedAgentError,
    api_base,
    model_of,
    resolve_agent,
)

AGENT_ID = "6aa93f1b3817a13d3103d2c1"
BASE = "http://localhost:8788/agentos/api"
URL = f"{BASE}/v1/agents/{AGENT_ID}"
KEY = "ags_testkey_do_not_use"


def _resolve(**kw):
    return resolve_agent(
        AGENT_ID, base_url=kw.pop("base_url", BASE), api_key=kw.pop("api_key", KEY), **kw
    )


# ── URL normalisation ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("http://h:8788", "http://h:8788/agentos/api/v1"),
        ("http://h:8788/", "http://h:8788/agentos/api/v1"),
        ("http://h:8788/agentos/api", "http://h:8788/agentos/api/v1"),
        ("http://h:8788/agentos/api/", "http://h:8788/agentos/api/v1"),
        ("http://h:8788/agentos/api/v1", "http://h:8788/agentos/api/v1"),
        ("http://h:8788/agentos/api/v1/", "http://h:8788/agentos/api/v1"),
    ],
)
def test_api_base_accepts_every_spelling(given, expected):
    assert api_base(given) == expected


def test_api_base_rejects_empty():
    with pytest.raises(AgentResolveError, match="base_url is empty"):
        api_base("/")


# ── happy path ───────────────────────────────────────────────────────────────


@respx.mock
def test_resolves_every_field(agent_doc):
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    agent = _resolve()

    assert agent.agent_id == AGENT_ID
    assert agent.name == "private git test with client"
    assert agent.harness == "claude-agent-sdk"
    assert agent.model == "6aa93f1a75ce74c4e6772b20"
    assert agent.gateway_type == "native"
    assert agent.has_native_gateway is True
    assert agent.gateway_base_url == "https://excellent-flavor-glance-myspace.trycloudflare.com"
    assert agent.gateway_completions_url.endswith("/v4/chat/completions")
    assert agent.agent_type == "client"


@respx.mock
def test_presents_the_key_on_x_api_key_and_never_as_bearer(agent_doc):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    _resolve()

    request = route.calls.last.request
    assert request.headers["x-api-key"] == KEY
    assert "authorization" not in request.headers


@respx.mock
def test_trailing_slash_on_base_url_does_not_double_the_path(agent_doc):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    _resolve(base_url=BASE + "/")

    assert str(route.calls.last.request.url) == URL


# ── model precedence ─────────────────────────────────────────────────────────


def test_external_model_id_wins(agent_doc):
    assert model_of(agent_doc) == "6aa93f1a75ce74c4e6772b20"


def test_falls_back_to_binding_model(agent_doc):
    agent_doc["model_gateway"]["binding"]["externalModelId"] = None
    assert model_of(agent_doc) == "Anthropic:anthropic/claude-sonnet-4-6"


def test_falls_back_to_top_level_model(agent_doc):
    agent_doc["model_gateway"]["binding"] = {"model": None, "externalModelId": None}
    assert model_of(agent_doc) == "Anthropic:anthropic/claude-sonnet-4-6"


def test_no_model_anywhere_is_none(agent_doc):
    agent_doc["model_gateway"] = None
    agent_doc["model"] = None
    assert model_of(agent_doc) is None


def test_blank_strings_are_not_a_model(agent_doc):
    agent_doc["model_gateway"]["binding"]["externalModelId"] = "   "
    assert model_of(agent_doc) == "Anthropic:anthropic/claude-sonnet-4-6"


# ── accepted shapes ──────────────────────────────────────────────────────────


@respx.mock
def test_null_gateway_is_allowed(agent_doc):
    """No gateway is a direct-Anthropic run, not an error."""
    agent_doc["model_gateway"] = None
    agent_doc["modelGatewayId"] = None
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    agent = _resolve()

    assert agent.gateway_type is None
    assert agent.has_native_gateway is False


@respx.mock
def test_gitagent_harness_is_allowed(agent_doc):
    agent_doc["harness"] = "gitagent"
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    assert _resolve().harness == "gitagent"


# ── refusals ─────────────────────────────────────────────────────────────────


@respx.mock
def test_refuses_generic_gateway(agent_doc):
    agent_doc["model_gateway"]["type"] = "generic"
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    with pytest.raises(UnsupportedAgentError, match="model_gateway.type"):
        _resolve()


@respx.mock
def test_bound_mcp_servers_are_resolved(agent_doc):
    agent_doc["mcp_servers"] = [
        {"server_id": "srv_1", "tool_name": "jira", "action_names": ["search"]}
    ]
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    agent = _resolve()

    assert agent.mcp_mode == "engine"
    assert [(b.server_id, b.tool_name, b.action_names) for b in agent.mcp_bindings] == [
        ("srv_1", "jira", ("search",))
    ]


@respx.mock
def test_a_binding_without_a_tool_name_is_refused_by_index(agent_doc):
    agent_doc["mcp_servers"] = [{"server_id": "srv_1", "action_names": ["x"]}]
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    with pytest.raises(UnsupportedAgentError, match=r"mcp_servers\[0\].*tool_name"):
        _resolve()


@respx.mock
def test_refuses_orchestration(agent_doc):
    agent_doc["orchestration"] = {"mode": "roster", "maxParallel": 4}
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    with pytest.raises(UnsupportedAgentError, match="orchestration"):
        _resolve()


@respx.mock
def test_an_unknown_harness_resolves_fine(agent_doc):
    """Resolution validates the DOCUMENT. Whether a harness can run depends on
    the caller's engine too, so that check lives at construction."""
    agent_doc["harness"] = "deepagents"
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    assert _resolve().harness == "deepagents"


def test_unknown_harness_is_refused_when_no_engine_is_given(agent_doc):
    from computeragent.harness.agentos_agent import require_runnable_harness

    agent = ResolvedAgent(
        agent_id="a",
        name="a",
        harness="deepagents",
        model=None,
        gateway_type=None,
        gateway_base_url=None,
        gateway_completions_url=None,
        agent_type=None,
    )

    with pytest.raises(UnsupportedAgentError, match="harness"):
        require_runnable_harness(agent, engine_given=False)


def test_bringing_your_own_engine_rescues_an_unknown_harness(agent_doc):
    """The error tells you to pass engine=. That has to actually work."""
    from computeragent.harness.agentos_agent import require_runnable_harness

    agent = ResolvedAgent(
        agent_id="a",
        name="a",
        harness="deepagents",
        model=None,
        gateway_type=None,
        gateway_base_url=None,
        gateway_completions_url=None,
        agent_type=None,
    )

    require_runnable_harness(agent, engine_given=True)


@respx.mock
def test_refuses_a_binding_whose_gateway_did_not_resolve(agent_doc):
    """`requireConsistentGatewayBinding`: the one case where the catalog name
    would reach the gateway and 500 it."""
    agent_doc["model_gateway"] = None  # gateway failed to resolve for this caller
    # modelGatewayId stays set — the agent still believes it is bound.
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    with pytest.raises(UnsupportedAgentError, match="did not resolve"):
        _resolve()


# ── HTTP errors ──────────────────────────────────────────────────────────────


@respx.mock
def test_401_points_at_the_pagos_bridge():
    respx.get(URL).mock(
        return_value=httpx.Response(401, json={"error": {"code": "UNAUTHENTICATED"}})
    )

    with pytest.raises(AgentResolveError, match="PAGOS"):
        _resolve()


@respx.mock
def test_403_insufficient_permission_names_the_permission():
    respx.get(URL).mock(
        return_value=httpx.Response(403, json={"error": {"code": "INSUFFICIENT_PERMISSION"}})
    )

    with pytest.raises(AgentResolveError, match="agents:read"):
        _resolve()


@respx.mock
def test_403_not_owner_names_the_group():
    respx.get(URL).mock(return_value=httpx.Response(403, json={"error": {"code": "NOT_OWNER"}}))

    with pytest.raises(AgentResolveError, match="owned by another group"):
        _resolve()


@respx.mock
def test_404_explains_id_vs_agent_id():
    respx.get(URL).mock(return_value=httpx.Response(404, json={"error": {"code": "NOT_FOUND"}}))

    with pytest.raises(AgentResolveError, match="24-hex Mongo"):
        _resolve()


@respx.mock
def test_500_is_reported_with_its_status():
    respx.get(URL).mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(AgentResolveError, match="HTTP 500"):
        _resolve()


@respx.mock
def test_connection_failure_is_wrapped_not_raw():
    respx.get(URL).mock(side_effect=httpx.ConnectError("no route"))

    with pytest.raises(AgentResolveError, match="could not reach AgentOS"):
        _resolve()


@respx.mock
def test_non_json_body_is_reported_clearly(agent_doc):
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))

    with pytest.raises(AgentResolveError, match="non-JSON"):
        _resolve()


# ── argument validation ──────────────────────────────────────────────────────


def test_missing_api_key_is_refused_before_any_request():
    with pytest.raises(AgentResolveError, match="api_key is required"):
        resolve_agent(AGENT_ID, base_url=BASE, api_key="")


def test_missing_agent_id_is_refused_before_any_request():
    with pytest.raises(AgentResolveError, match="agent_id is required"):
        resolve_agent("  ", base_url=BASE, api_key=KEY)


# ── secret hygiene ───────────────────────────────────────────────────────────


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"code": "UNAUTHENTICATED"}}),
        httpx.Response(403, json={"error": {"code": "NOT_OWNER"}}),
        httpx.Response(404, json={"error": {"code": "NOT_FOUND"}}),
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
    ],
)
def test_the_key_never_appears_in_an_error_message(response):
    respx.get(URL).mock(return_value=response)

    with pytest.raises(AgentResolveError) as excinfo:
        _resolve()

    assert KEY not in str(excinfo.value)


@respx.mock
def test_the_key_never_appears_in_the_resolved_repr(agent_doc):
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    assert KEY not in repr(_resolve())
