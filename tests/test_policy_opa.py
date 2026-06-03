"""``OpaPolicyEngine`` — respx-mocked OPA REST API."""

from __future__ import annotations

import httpx
import pytest
import respx

from computeragent.policy import (
    PolicyAction,
    PolicyDecision,
    PolicyInput,
    PolicyPrincipal,
    PolicyResource,
)
from computeragent.policy.opa import OpaPolicyEngine

OPA_URL = "http://opa.test:8181"
POLICY_PATH = "computeragent/tools/allow"
ENDPOINT = f"{OPA_URL}/v1/data/computeragent/tools/allow"


def _input(tool: str = "Read") -> PolicyInput:
    return PolicyInput(
        principal=PolicyPrincipal(id="alice", groups=["engineer"]),
        action=PolicyAction(name="tool_use", tool_name=tool),
        resource=PolicyResource(agent_name="nord"),
        context={"env": "prod"},
    )


@pytest.mark.asyncio
async def test_bare_true_response_allows():
    async with respx.mock(base_url=OPA_URL) as router:
        router.post("/v1/data/computeragent/tools/allow").mock(
            return_value=httpx.Response(200, json={"result": True})
        )
        engine = OpaPolicyEngine(url=OPA_URL, policy_path=POLICY_PATH)
        result = await engine.evaluate(_input())
        assert result.decision == PolicyDecision.ALLOW
        assert result.engine == "opa"
        await engine.aclose()


@pytest.mark.asyncio
async def test_bare_false_response_denies():
    async with respx.mock(base_url=OPA_URL) as router:
        router.post("/v1/data/computeragent/tools/allow").mock(
            return_value=httpx.Response(200, json={"result": False})
        )
        engine = OpaPolicyEngine(url=OPA_URL, policy_path=POLICY_PATH)
        result = await engine.evaluate(_input("Bash"))
        assert result.decision == PolicyDecision.DENY
        await engine.aclose()


@pytest.mark.asyncio
async def test_object_response_with_reason_and_obligations():
    async with respx.mock(base_url=OPA_URL) as router:
        router.post("/v1/data/computeragent/tools/allow").mock(
            return_value=httpx.Response(
                200,
                json={
                    "result": {
                        "allow": False,
                        "reason": "tool not on engineer allowlist",
                        "obligations": [{"audit": "deny"}],
                    }
                },
            )
        )
        engine = OpaPolicyEngine(url=OPA_URL, policy_path=POLICY_PATH)
        result = await engine.evaluate(_input("Bash"))
        assert result.decision == PolicyDecision.DENY
        assert "engineer allowlist" in result.reason
        assert result.obligations == [{"audit": "deny"}]
        await engine.aclose()


@pytest.mark.asyncio
async def test_dotted_policy_path_is_normalised_to_slashes():
    """``policy_path="computeragent.tools.allow"`` works just like the slash form."""
    async with respx.mock(base_url=OPA_URL) as router:
        route = router.post("/v1/data/computeragent/tools/allow").mock(
            return_value=httpx.Response(200, json={"result": True})
        )
        engine = OpaPolicyEngine(url=OPA_URL, policy_path="computeragent.tools.allow")
        await engine.evaluate(_input())
        assert route.called
        await engine.aclose()


@pytest.mark.asyncio
async def test_request_body_carries_input_under_input_key():
    captured: dict = {}

    async with respx.mock(base_url=OPA_URL) as router:

        def _capture(request: httpx.Request):
            captured["json"] = request.read().decode()
            return httpx.Response(200, json={"result": True})

        router.post("/v1/data/computeragent/tools/allow").mock(side_effect=_capture)
        engine = OpaPolicyEngine(url=OPA_URL, policy_path=POLICY_PATH)
        await engine.evaluate(_input("Read"))
        await engine.aclose()

    import json as _json

    body = _json.loads(captured["json"])
    assert "input" in body
    assert body["input"]["action"]["tool_name"] == "Read"
    assert body["input"]["principal"]["id"] == "alice"


@pytest.mark.asyncio
async def test_http_500_fails_closed_by_default():
    async with respx.mock(base_url=OPA_URL) as router:
        router.post(ENDPOINT).mock(return_value=httpx.Response(500, text="oops"))
        engine = OpaPolicyEngine(url=OPA_URL, policy_path=POLICY_PATH)
        result = await engine.evaluate(_input())
        assert result.decision == PolicyDecision.DENY
        assert "unavailable" in result.reason
        await engine.aclose()


@pytest.mark.asyncio
async def test_http_500_fails_open_when_configured():
    async with respx.mock(base_url=OPA_URL) as router:
        router.post(ENDPOINT).mock(return_value=httpx.Response(500))
        engine = OpaPolicyEngine(url=OPA_URL, policy_path=POLICY_PATH, fail_mode="allow")
        result = await engine.evaluate(_input())
        assert result.decision == PolicyDecision.ALLOW
        await engine.aclose()


@pytest.mark.asyncio
async def test_timeout_fails_closed():
    async with respx.mock(base_url=OPA_URL) as router:
        router.post(ENDPOINT).mock(side_effect=httpx.ReadTimeout("slow"))
        engine = OpaPolicyEngine(url=OPA_URL, policy_path=POLICY_PATH, timeout=0.1)
        result = await engine.evaluate(_input())
        assert result.decision == PolicyDecision.DENY
        await engine.aclose()


@pytest.mark.asyncio
async def test_malformed_json_fails_closed():
    async with respx.mock(base_url=OPA_URL) as router:
        router.post(ENDPOINT).mock(
            return_value=httpx.Response(
                200, content=b"not json", headers={"content-type": "application/json"}
            )
        )
        engine = OpaPolicyEngine(url=OPA_URL, policy_path=POLICY_PATH)
        result = await engine.evaluate(_input())
        assert result.decision == PolicyDecision.DENY
        await engine.aclose()


@pytest.mark.asyncio
async def test_unexpected_result_shape_fails_closed():
    async with respx.mock(base_url=OPA_URL) as router:
        router.post(ENDPOINT).mock(return_value=httpx.Response(200, json={"result": [1, 2, 3]}))
        engine = OpaPolicyEngine(url=OPA_URL, policy_path=POLICY_PATH)
        result = await engine.evaluate(_input())
        assert result.decision == PolicyDecision.DENY
        await engine.aclose()


@pytest.mark.asyncio
async def test_injected_client_is_not_closed_by_engine():
    """Engines that did not create their client must not close it on aclose."""
    client = httpx.AsyncClient()
    engine = OpaPolicyEngine(url=OPA_URL, policy_path=POLICY_PATH, client=client)
    await engine.aclose()
    assert not client.is_closed
    await client.aclose()
