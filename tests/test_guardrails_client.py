"""Guardrail checks through the egress proxy: ``POST {guardrails_url}/v1/check``.

Bodies and answers follow the agent gateway contract (``agent-gateway-v1``
§7.3); verdict mapping is plan D5, no-verdict D6, the tool-use id D7.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from computeragent.policy.guardrails_client import (
    GuardrailsUnavailable,
    TextVerdict,
    Violation,
    check_text,
    check_tool_call,
    mcp_server_names_of,
    policy_tool_name,
)

GUARDRAILS = "http://egress.internal/guardrails"
CHECK = f"{GUARDRAILS}/v1/check"
KEY = "sbt_key"
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "guardrails-input.json").read_text())


def answer(**data):
    base = {
        "decision": "allow",
        "op": "input",
        "text": None,
        "blockReason": None,
        "blockedByPolicyId": None,
        "results": [],
        "policyIds": ["pol_1"],
        "warnings": 0,
        "skipped": False,
    }
    base.update(data)
    return httpx.Response(200, json={"data": base})


def body_of(route) -> dict:
    return json.loads(route.calls.last.request.content)


# ── input ────────────────────────────────────────────────────────────────────


@respx.mock
async def test_check_text_body_path_and_key():
    route = respx.post(CHECK).mock(return_value=answer())

    await check_text(GUARDRAILS, api_key=KEY, text="hello")

    assert body_of(route) == {"op": "input", "subject": {"text": "hello"}}
    assert route.calls.last.request.headers["x-api-key"] == KEY


@respx.mock
async def test_rewrite_from_the_contract_fixture():
    respx.post(CHECK).mock(return_value=httpx.Response(200, json=FIXTURE))

    verdict = await check_text(GUARDRAILS, api_key=KEY, text="mail me at a@b.com")

    assert verdict == TextVerdict(text="mail me at [EMAIL]", blocked=False, outcome="redacted")


@respx.mock
async def test_allow_keeps_the_text():
    respx.post(CHECK).mock(return_value=answer(decision="allow"))

    verdict = await check_text(GUARDRAILS, api_key=KEY, text="hi")

    assert verdict == TextVerdict(text="hi", blocked=False, outcome="clean")


@respx.mock
async def test_block_carries_reason_and_policy():
    respx.post(CHECK).mock(
        return_value=answer(decision="block", blockReason="nope", blockedByPolicyId="pol_x")
    )

    verdict = await check_text(GUARDRAILS, api_key=KEY, text="x")

    assert verdict.blocked is True
    assert verdict.outcome == "blocked"
    assert verdict.reason == "nope"
    assert verdict.blocked_by_policy_id == "pol_x"


@respx.mock
async def test_warnings_are_logged_by_count(caplog):
    respx.post(CHECK).mock(return_value=answer(warnings=2))

    with caplog.at_level("WARNING", logger="computeragent.policy.guardrails"):
        await check_text(GUARDRAILS, api_key=KEY, text="secret words")

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "2 warning(s)" in messages
    assert "secret words" not in messages


@respx.mock
async def test_skipped_is_an_allow(caplog):
    respx.post(CHECK).mock(return_value=answer(decision="allow", skipped=True))

    with caplog.at_level("INFO", logger="computeragent.policy.guardrails"):
        verdict = await check_text(GUARDRAILS, api_key=KEY, text="x")

    assert verdict.blocked is False
    assert verdict.skipped is True
    assert any("fail-open by the gateway" in r.getMessage() for r in caplog.records)


@respx.mock
async def test_unknown_decision_blocks_the_text():
    respx.post(CHECK).mock(return_value=answer(decision="maybe"))

    verdict = await check_text(GUARDRAILS, api_key=KEY, text="x")

    assert verdict.blocked is True


# ── tool calls ───────────────────────────────────────────────────────────────


async def run_tool(**kw):
    params = dict(
        api_key=KEY,
        call_id="toolu_1",
        tool_name="mcp__srv__act",
        tool_args={"q": "x", "n": 2},
    )
    params.update(kw)
    return await check_tool_call(GUARDRAILS, **params)


@respx.mock
async def test_check_tool_call_body_keeps_the_engine_id():
    route = respx.post(CHECK).mock(return_value=answer(op="tool-input"))

    assert await run_tool() is None

    assert body_of(route) == {
        "op": "tool-input",
        "subject": {
            "toolCalls": [
                {"id": "toolu_1", "name": "mcp__srv__act", "arguments": {"q": "x", "n": 2}}
            ]
        },
    }


@respx.mock
async def test_a_blocked_result_is_a_violation():
    respx.post(CHECK).mock(
        return_value=answer(
            decision="block",
            op="tool-input",
            results=[
                {
                    "id": "toolu_1",
                    "decision": "block",
                    "deniedBy": "cedar",
                    "reason": "rm is forbidden",
                    "policyId": "pol_c",
                }
            ],
        )
    )

    assert await run_tool() == Violation(
        denied_by="cedar", reason="rm is forbidden", policy_id="pol_c"
    )


@respx.mock
async def test_a_block_without_results_falls_back_to_the_top_level():
    respx.post(CHECK).mock(
        return_value=answer(decision="block", blockReason="no", blockedByPolicyId="pol_t")
    )

    assert await run_tool() == Violation(denied_by="guardrails", reason="no", policy_id="pol_t")


@pytest.mark.parametrize("decision", ["allow", "rewrite"])
@respx.mock
async def test_allow_and_rewrite_allow_the_call(decision):
    respx.post(CHECK).mock(return_value=answer(decision=decision))

    assert await run_tool() is None


@respx.mock
async def test_unknown_decision_denies_the_call():
    respx.post(CHECK).mock(return_value=answer(decision="maybe"))

    violation = await run_tool()

    assert violation is not None
    assert violation.denied_by == "guardrails"


@respx.mock
async def test_an_empty_id_is_refused_before_any_request():
    route = respx.post(CHECK).mock(return_value=answer())

    with pytest.raises(ValueError, match="tool-use id"):
        await run_tool(call_id="")
    assert route.call_count == 0


# ── no verdict ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [400, 413, 500, 502, 503])
@respx.mock
async def test_a_non_2xx_is_no_verdict(status):
    respx.post(CHECK).mock(
        return_value=httpx.Response(
            status, json={"error": {"code": "rai_unavailable", "message": "echo of text"}}
        )
    )

    with pytest.raises(GuardrailsUnavailable) as exc:
        await check_text(GUARDRAILS, api_key=KEY, text="x")

    assert exc.value.status == status
    assert str(exc.value) == f"guardrails {status} (rai_unavailable)"
    assert "echo of text" not in str(exc.value)


@respx.mock
async def test_a_400_is_logged_as_an_error(caplog):
    respx.post(CHECK).mock(return_value=httpx.Response(400))

    with (
        caplog.at_level("ERROR", logger="computeragent.policy.guardrails"),
        pytest.raises(GuardrailsUnavailable),
    ):
        await check_text(GUARDRAILS, api_key=KEY, text="x")

    assert any(r.levelname == "ERROR" for r in caplog.records)


@pytest.mark.parametrize("error", [httpx.ConnectError("boom"), httpx.ReadTimeout("slow")])
@respx.mock
async def test_unreachable_is_no_verdict(error):
    respx.post(CHECK).mock(side_effect=error)

    with pytest.raises(GuardrailsUnavailable) as exc:
        await check_text(GUARDRAILS, api_key=KEY, text="x")

    assert exc.value.status is None
    assert str(exc.value).startswith("guardrails unreachable: ")


@pytest.mark.parametrize("body", ["not json", "[1]", '{"nodata": 1}'])
@respx.mock
async def test_a_malformed_answer_is_no_verdict(body):
    respx.post(CHECK).mock(return_value=httpx.Response(200, text=body))

    with pytest.raises(GuardrailsUnavailable, match="malformed"):
        await check_text(GUARDRAILS, api_key=KEY, text="x")


@respx.mock
async def test_never_logs_text_arguments_or_key(caplog):
    respx.post(CHECK).mock(return_value=httpx.Response(500, text="body echo"))

    with (
        caplog.at_level("DEBUG", logger="computeragent.policy.guardrails"),
        pytest.raises(GuardrailsUnavailable),
    ):
        await run_tool(tool_args={"command": "cat /secret/file"})

    logged = " ".join(r.getMessage() for r in caplog.records)
    for secret in (KEY, "/secret/file", "body echo"):
        assert secret not in logged


@respx.mock
async def test_a_trailing_slash_on_the_url_does_not_double_up():
    route = respx.post(CHECK).mock(return_value=answer())

    await check_text(GUARDRAILS + "/", api_key=KEY, text="x")

    assert route.called


# ── naming helpers (moved from the SRS client) ───────────────────────────────


@pytest.mark.parametrize(
    ("tool", "engine", "expected"),
    [
        ("Bash", "claude-agent-sdk", "claude-agent-sdk__Bash"),
        ("write", "gitagent", "gitagent__write"),
        ("foo__bar", "gitagent", "gitagent__foo__bar"),
        ("mcp__probe__get_build_info", "claude-agent-sdk", "mcp__probe__get_build_info"),
        ("probe__get_build_info", "gitagent", "mcp__probe__get_build_info"),
    ],
)
def test_policy_tool_name(tool, engine, expected):
    assert policy_tool_name(tool, engine_name=engine, mcp_server_names=["probe"]) == expected


def test_mcp_server_names_of_tolerates_any_shape():
    assert mcp_server_names_of({"mcp_servers": {"jira": {}, "gh": {}}}) == ["jira", "gh"]
    assert mcp_server_names_of({"mcp_servers": []}) == []
    assert mcp_server_names_of(None) == []


@pytest.mark.parametrize("injected", [False, True])
@respx.mock
async def test_reissue_adopted(injected):
    from datetime import datetime, timedelta, timezone

    from computeragent.harness.egress_token import CREDENTIAL

    CREDENTIAL.configure([GUARDRAILS])
    expiry = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
    respx.post(CHECK).mock(
        return_value=httpx.Response(
            200,
            json={"data": {"decision": "allow"}},
            headers={"x-cas-token": "sbt_next", "x-cas-token-expires-at": expiry},
        )
    )
    client = httpx.AsyncClient() if injected else None

    await check_text(GUARDRAILS, api_key="sbt_first", text="x", client=client)

    assert CREDENTIAL.resolve("sbt_first") == "sbt_next"
    route = respx.post(CHECK).mock(return_value=answer())
    await check_text(GUARDRAILS, api_key="sbt_first", text="x", client=client)
    assert route.calls.last.request.headers["x-api-key"] == "sbt_next"
