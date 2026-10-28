"""Org (global) RAI policies from ``GET {agent_api_url}/v3/org/rai-policy``.

Ported from CAS ``org-rai-policy.ts`` and the org cases of
``srs-policy-decider.test.ts``.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from computeragent.policy.org_policy import (
    OrgPolicyFetchError,
    OrgRaiPolicy,
    fetch_org_rai_policies,
)

AGENT_API = "http://egress.internal/agent-api"
URL = f"{AGENT_API}/v3/org/rai-policy"
KEY = "sbx_key"


def mock(status=200, **kw):
    return respx.get(URL).mock(return_value=httpx.Response(status, **kw))


@respx.mock
async def test_reads_the_list_shape_in_priority_order_with_the_key():
    route = mock(
        json={
            "org_id": "o",
            "policies": [
                {"policy_id": "p1", "enabled": True, "fail_open": True, "timeout": 30},
                {"policy_id": "p2", "enabled": True},
            ],
        }
    )

    policies = await fetch_org_rai_policies(AGENT_API, KEY)

    assert policies == [
        OrgRaiPolicy(policy_id="p1", fail_open=True, timeout_s=30.0, dangling=False),
        OrgRaiPolicy(policy_id="p2", fail_open=False, timeout_s=100.0, dangling=False),
    ]
    assert route.calls.last.request.headers["x-api-key"] == KEY


@respx.mock
async def test_reads_the_legacy_single_policy_shape():
    mock(json={"org_id": "o", "policy_id": "legacy", "enabled": True, "fail_open": False})

    assert [p.policy_id for p in await fetch_org_rai_policies(AGENT_API, KEY)] == ["legacy"]


@respx.mock
async def test_a_policies_list_is_authoritative_over_the_legacy_fields():
    mock(json={"policy_id": "legacy", "enabled": True, "policies": []})

    assert await fetch_org_rai_policies(AGENT_API, KEY) == []


@respx.mock
async def test_an_enabled_false_row_is_no_policy_even_with_an_id():
    mock(json={"policies": [{"policy_id": "p1", "enabled": False}]})

    assert await fetch_org_rai_policies(AGENT_API, KEY) == []


@respx.mock
async def test_the_none_shape_is_no_policy():
    mock(json={"org_id": "o", "enabled": False, "policy_id": None})

    assert await fetch_org_rai_policies(AGENT_API, KEY) == []


@respx.mock
async def test_a_blank_policy_id_is_no_policy():
    mock(json={"policies": [{"policy_id": "  ", "enabled": True}]})

    assert await fetch_org_rai_policies(AGENT_API, KEY) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(30, 30.0), (0.5, 0.5), (100000, 100000.0), ("45", 45.0)],
)
@respx.mock
async def test_any_positive_timeout_is_honoured_with_no_upper_cap(raw, expected):
    mock(json={"policies": [{"policy_id": "p", "enabled": True, "timeout": raw}]})

    (policy,) = await fetch_org_rai_policies(AGENT_API, KEY)
    assert policy.timeout_s == expected


@pytest.mark.parametrize("raw", [None, 0, -5, "soon", float("inf")])
@respx.mock
async def test_an_absent_or_invalid_timeout_falls_back_to_100s(raw):
    row = {"policy_id": "p", "enabled": True}
    if raw is not None:
        row["timeout"] = raw if raw != float("inf") else "inf"
    mock(json={"policies": [row]})

    (policy,) = await fetch_org_rai_policies(AGENT_API, KEY)
    assert policy.timeout_s == 100.0


@respx.mock
async def test_a_dangling_policy_is_kept_and_warned_about(caplog):
    mock(json={"policies": [{"policy_id": "gone", "enabled": True, "policy_exists": False}]})

    with caplog.at_level("WARNING", logger="computeragent.policy.org"):
        (policy,) = await fetch_org_rai_policies(AGENT_API, KEY)

    assert policy.dangling is True
    assert any("gone" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("status", [400, 403])
@respx.mock
async def test_400_and_403_mean_no_org_context_not_an_outage(status):
    mock(status, json={"error": "no org"})

    assert await fetch_org_rai_policies(AGENT_API, KEY) == []


@pytest.mark.parametrize("status", [401, 404, 500, 503])
@respx.mock
async def test_any_other_status_raises(status):
    mock(status, text="request echo with secrets")

    with pytest.raises(OrgPolicyFetchError, match=str(status)) as exc:
        await fetch_org_rai_policies(AGENT_API, KEY)
    assert "secrets" not in str(exc.value)


@respx.mock
async def test_a_network_failure_raises():
    respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(OrgPolicyFetchError, match="ConnectError"):
        await fetch_org_rai_policies(AGENT_API, KEY)


@pytest.mark.parametrize("body", ["not json", "[1, 2]", "null"])
@respx.mock
async def test_malformed_json_raises(body):
    mock(text=body)

    with pytest.raises(OrgPolicyFetchError, match="malformed JSON"):
        await fetch_org_rai_policies(AGENT_API, KEY)


@respx.mock
async def test_no_key_means_no_org_and_no_request():
    route = mock(json={"policies": []})

    assert await fetch_org_rai_policies(AGENT_API, "") == []
    assert route.call_count == 0


@respx.mock
async def test_the_key_never_appears_in_an_error():
    respx.get(URL).mock(side_effect=httpx.ConnectError(f"boom {KEY}"))

    with pytest.raises(OrgPolicyFetchError) as exc:
        await fetch_org_rai_policies(AGENT_API, KEY)
    assert KEY not in str(exc.value)


@respx.mock
async def test_a_trailing_slash_on_the_base_does_not_double_up():
    route = mock(json={"policies": []})

    await fetch_org_rai_policies(AGENT_API + "/", KEY)
    assert route.called
