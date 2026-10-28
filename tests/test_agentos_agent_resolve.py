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
    assert agent.model == "claude-sonnet-4-5"
    assert agent.agent_type == "client"
    assert agent.mcp_bindings == ()
    for gone in ("gateway_type", "gateway_base_url", "gateway_completions_url", "mcp_mode"):
        assert not hasattr(agent, gone)


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


@respx.mock
def test_rai_policy_id_ignored(agent_doc):
    """Policies are applied by the gateway behind the guardrails check; the
    SDK reads no policy id from the agent document."""
    respx.get(URL).mock(return_value=httpx.Response(200, json={**agent_doc, "rai_policy_id": "p1"}))

    agent = _resolve()

    assert not hasattr(agent, "rai_policy_id")


EGRESS_BASE = "http://egress/computer-agent-api/agentos/api"


@pytest.mark.parametrize("base", [EGRESS_BASE, EGRESS_BASE + "/", EGRESS_BASE + "/v1"])
@respx.mock
def test_egress_base_builds_single_prefix(agent_doc, base):
    """Behind the egress proxy the agent document is read from its
    /computer-agent-api prefix, with exactly one /agentos/api."""
    route = respx.get(f"{EGRESS_BASE}/v1/agents/{AGENT_ID}").mock(
        return_value=httpx.Response(200, json=agent_doc)
    )

    _resolve(base_url=base)

    request = route.calls.last.request
    assert str(request.url) == f"{EGRESS_BASE}/v1/agents/{AGENT_ID}"
    assert request.headers["x-api-key"] == KEY


# ── the model ────────────────────────────────────────────────────────────────


def test_model_from_llm_config(agent_doc):
    assert model_of(agent_doc) == "claude-sonnet-4-5"


@pytest.mark.parametrize(
    "llm",
    [None, "absent", {}, {"config": None}, {"config": {"model": "  "}}, {"config": "x"}, "kong"],
)
def test_null_llm_uses_the_image_model(agent_doc, llm):
    from computeragent.harness.gateway_envs import build_options

    if llm == "absent":
        del agent_doc["llm"]
    else:
        agent_doc["llm"] = llm
    assert model_of(agent_doc) is None

    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))
        agent = _resolve()
    assert "model" not in build_options(agent, engine_name="claude-agent-sdk")


# ── accepted shapes ──────────────────────────────────────────────────────────


@respx.mock
def test_legacy_fields_are_ignored(agent_doc):
    """A pre-#164 document resolves with no model and no MCP servers."""
    agent_doc.update(
        {
            "llm": None,
            "mcp": None,
            "model": "Anthropic:anthropic/claude-sonnet-4-6",
            "model_gateway": {"type": "generic", "binding": {"externalModelId": "x"}},
            "modelGatewayId": "6a9a6c9893432debd1668743",
            "mcp_servers": [{"server_id": "srv_1", "tool_name": "jira"}],
            "mcp_mode": "studio",
        }
    )
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    agent = _resolve()

    assert agent.model is None
    assert agent.mcp_bindings == ()


@respx.mock
def test_guardrails_section_ignored(agent_doc):
    agent_doc["guardrails"] = {"provider": "lyzr-srs", "config": {"id": "pol_1"}}
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    agent = _resolve()

    assert route.call_count == 1
    assert not any("guardrail" in f or "polic" in f for f in vars(agent))


@respx.mock
def test_gitagent_harness_is_allowed(agent_doc):
    agent_doc["harness"] = "gitagent"
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    assert _resolve().harness == "gitagent"


# ── refusals ─────────────────────────────────────────────────────────────────


@respx.mock
def test_bound_mcp_servers_are_resolved(agent_doc):
    agent_doc["mcp"] = {
        "provider": "lyzr-mcp",
        "servers": [
            {
                "server_id": "srv_1",
                "tool_name": "jira",
                "action_names": ["search"],
                "persist_auth": True,
                "needs_your_connection": False,
                "your_server_id": None,
            }
        ],
    }
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    agent = _resolve()

    assert [(b.server_id, b.tool_name, b.action_names) for b in agent.mcp_bindings] == [
        ("srv_1", "jira", ("search",))
    ]


@respx.mock
def test_a_binding_without_a_name_refuses_the_agent(agent_doc):
    agent_doc["mcp"] = {"servers": [{"server_id": "s"}]}
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    with pytest.raises(UnsupportedAgentError, match=r"mcp\.servers\[0\].*tool_name"):
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
        agent_type=None,
    )

    require_runnable_harness(agent, engine_given=True)


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


def _reissue_headers():
    from datetime import datetime, timedelta, timezone

    expiry = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
    return {"x-cas-token": "sbt_next", "x-cas-token-expires-at": expiry}


@pytest.mark.parametrize("injected", [False, True])
@respx.mock
def test_reissue_adopted(agent_doc, injected):
    from computeragent.harness.egress_token import CREDENTIAL

    CREDENTIAL.configure([BASE])
    respx.get(URL).mock(
        return_value=httpx.Response(200, json=agent_doc, headers=_reissue_headers())
    )
    client = httpx.Client() if injected else None

    resolve_agent(AGENT_ID, base_url=BASE, api_key="sbt_first", client=client)

    assert CREDENTIAL.resolve("sbt_first") == "sbt_next"
