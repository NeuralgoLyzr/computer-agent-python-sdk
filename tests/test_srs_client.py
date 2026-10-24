"""The SRS v1 calls, ported from CAS harness-server ``srs-policy-decider.ts``
and ``policy-tool-name.ts`` (test vectors included)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from computeragent.policy.srs_client import (
    InputVerdict,
    SrsUnavailable,
    Violation,
    check_input,
    check_tool_call,
    check_tool_content,
    mcp_server_names_of,
    policy_tool_name,
    tool_args_text_blob,
)

SRS = "http://egress.internal/srs"
INFERENCE = f"{SRS}/v1/rai/inference"
TOOL_CALL = f"{SRS}/v1/guardrails/evaluate-tool-call"
KEY = "sbx_key"
POLICIES = ["org1", "local1"]


def body_of(route) -> dict:
    return json.loads(route.calls.last.request.content)


async def run_input(text="mail me at a@b.com", **kw):
    return await check_input(
        SRS,
        api_key=KEY,
        policy_ids=POLICIES,
        text=text,
        principal_id="my-agent",
        timeout_s=5,
        **kw,
    )


async def run_content(args=None, **kw):
    params = dict(
        api_key=KEY,
        policy_ids=POLICIES,
        tool_name="claude-agent-sdk__Bash",
        tool_args={"command": "rm -rf /tmp/x"} if args is None else args,
        call_id="toolu_1",
        agent_name="my-agent",
        session_id="sess-1",
        principal_id="my-agent",
        timeout_s=5,
    )
    params.update(kw)
    return await check_tool_content(SRS, **params)


async def run_tool_call(**kw):
    params = dict(
        api_key=KEY,
        policy_ids=POLICIES,
        tool_name="claude-agent-sdk__Bash",
        raw_tool_name="Bash",
        tool_args={"command": "ls", "timeout": 5},
        agent_name="my-agent",
        principal_id="my-agent",
        timeout_s=5,
    )
    params.update(kw)
    return await check_tool_call(SRS, **params)


# ── input check ──────────────────────────────────────────────────────────────


@respx.mock
async def test_input_request_shape():
    route = respx.post(INFERENCE).mock(return_value=httpx.Response(200, json={}))

    await run_input()

    assert body_of(route) == {
        "policy_ids": POLICIES,
        "input_text": "mail me at a@b.com",
        "stage": "llm_input",
        "principal_id": "my-agent",
    }
    assert route.calls.last.request.headers["x-api-key"] == KEY


@respx.mock
async def test_input_masked():
    respx.post(INFERENCE).mock(
        return_value=httpx.Response(
            200,
            json={
                "processed_text": "mail me at <EMAIL_ADDRESS_1>",
                "blocked": False,
                "redaction_map": {"a@b.com": "<EMAIL_ADDRESS_1>"},
            },
        )
    )

    verdict = await run_input()

    assert verdict == InputVerdict(
        text="mail me at <EMAIL_ADDRESS_1>",
        blocked=False,
        outcome="redacted",
        entity_counts={"EMAIL_ADDRESS": 1},
    )


@respx.mock
async def test_input_blocked_names_the_policy():
    respx.post(INFERENCE).mock(
        return_value=httpx.Response(
            200,
            json={"blocked": True, "block_reason": "injection", "blocked_by_policy_id": "local1"},
        )
    )

    verdict = await run_input()

    assert verdict.blocked is True
    assert verdict.outcome == "blocked"
    assert verdict.reason == "injection"
    assert verdict.blocked_by_policy_id == "local1"


@respx.mock
async def test_input_clean_keeps_the_text():
    respx.post(INFERENCE).mock(return_value=httpx.Response(200, json={"blocked": False}))

    verdict = await run_input("hello")

    assert verdict.text == "hello"
    assert verdict.outcome == "clean"


@respx.mock
async def test_input_entity_counts_never_read_the_raw_values():
    respx.post(INFERENCE).mock(
        return_value=httpx.Response(
            200,
            json={
                "processed_text": "x",
                "redaction_map": {"secret@x.com": "<EMAIL_ADDRESS_1>", "555-1234": "weird"},
            },
        )
    )

    verdict = await run_input("y")

    # A token not shaped <TYPE_N> counts as PII, as CAS's entityTypeFromToken does.
    assert verdict.entity_counts == {"EMAIL_ADDRESS": 1, "PII": 1}
    assert "secret@x.com" not in repr(verdict)


# ── content check ────────────────────────────────────────────────────────────


@respx.mock
async def test_content_request_shape():
    route = respx.post(INFERENCE).mock(return_value=httpx.Response(200, json={}))

    await run_content({"command": "rm -rf /tmp/x", "cwd": "/w", "timeout": 5})

    assert body_of(route) == {
        "policy_ids": POLICIES,
        "input_text": "rm -rf /tmp/x\n/w",
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "toolu_1",
                        "type": "function",
                        "function": {
                            "name": "claude-agent-sdk__Bash",
                            "arguments": json.dumps(
                                {"command": "rm -rf /tmp/x", "cwd": "/w", "timeout": 5}
                            ),
                        },
                    }
                ],
            }
        ],
        "agent_id": "my-agent",
        "session_id": "sess-1",
        "stage": "llm_output",
        "use_cache": False,
        "principal_id": "my-agent",
    }


@respx.mock
async def test_content_no_string_arguments_makes_no_request():
    route = respx.post(INFERENCE).mock(return_value=httpx.Response(200, json={}))

    assert await run_content({"amount": 50000, "confirm": True}) is None
    assert route.call_count == 0


@respx.mock
async def test_content_without_a_call_id_omits_it():
    route = respx.post(INFERENCE).mock(return_value=httpx.Response(200, json={}))

    await run_content(call_id=None)

    assert "id" not in body_of(route)["messages"][0]["tool_calls"][0]


@respx.mock
async def test_content_passed():
    respx.post(INFERENCE).mock(
        return_value=httpx.Response(200, json={"blocked": False, "processed_text": "rewritten"})
    )

    assert await run_content() is None


@respx.mock
async def test_content_blocked():
    respx.post(INFERENCE).mock(
        return_value=httpx.Response(
            200, json={"blocked": True, "block_reason": "toxic", "blocked_by_policy_id": "org1"}
        )
    )

    assert await run_content() == Violation(denied_by="rai", reason="toxic", policy_id="org1")


@respx.mock
async def test_content_blocked_without_a_reason_falls_back():
    respx.post(INFERENCE).mock(return_value=httpx.Response(200, json={"blocked": True}))

    violation = await run_content()

    assert violation is not None
    assert violation.reason == "tool arguments blocked by content policy"


# ── tool-call check ──────────────────────────────────────────────────────────


@respx.mock
async def test_tool_call_request_shape():
    route = respx.post(TOOL_CALL).mock(return_value=httpx.Response(200, json={"allowed": True}))

    assert await run_tool_call() is None

    assert body_of(route) == {
        "policy_ids": POLICIES,
        "tool_name": "claude-agent-sdk__Bash",
        "raw_tool_name": "Bash",
        "tool_args": {"command": "ls", "timeout": 5},
        "principal_id": "my-agent",
        "bundle_id": "my-agent",
        "bundle_name": "my-agent",
    }
    assert route.calls.last.request.headers["x-api-key"] == KEY


@respx.mock
async def test_tool_call_denied_names_the_engine_and_policy():
    respx.post(TOOL_CALL).mock(
        return_value=httpx.Response(
            200,
            json={
                "allowed": False,
                "denied_by": "cedar",
                "reason": "rm is forbidden",
                "denied_by_policy_id": "local1",
            },
        )
    )

    assert await run_tool_call() == Violation(
        denied_by="cedar", reason="rm is forbidden", policy_id="local1"
    )


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({}, "SRS returned no `allowed` field"),
        ({"allowed": "yes"}, "SRS returned no `allowed` field"),
        ({"allowed": False}, "denied by RAI policy"),
    ],
)
@respx.mock
async def test_anything_but_allowed_true_is_a_deny(body, reason):
    respx.post(TOOL_CALL).mock(return_value=httpx.Response(200, json=body))

    violation = await run_tool_call()

    assert violation == Violation(denied_by="srs", reason=reason)


# ── no verdict ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "permanent"), [(403, True), (404, True), (422, True), (500, False), (503, False)]
)
@respx.mock
async def test_a_non_2xx_is_no_verdict(status, permanent):
    respx.post(TOOL_CALL).mock(return_value=httpx.Response(status, text="echo of tool args"))

    with pytest.raises(SrsUnavailable) as exc:
        await run_tool_call()

    assert exc.value.status == status
    assert exc.value.permanent is permanent
    assert str(exc.value) == f"SRS {status}"


@respx.mock
async def test_a_422_reports_machine_codes_never_the_detail():
    respx.post(INFERENCE).mock(
        return_value=httpx.Response(
            422,
            json={"detail": [{"type": "missing", "msg": "secret prompt text", "loc": ["x"]}]},
        )
    )

    with pytest.raises(SrsUnavailable) as exc:
        await run_input()

    assert str(exc.value) == "SRS 422 (missing)"
    assert "secret prompt text" not in str(exc.value)


@respx.mock
async def test_unreachable_is_no_verdict_with_no_status():
    respx.post(INFERENCE).mock(side_effect=httpx.ConnectError("boom http://user:pw@host"))

    with pytest.raises(SrsUnavailable) as exc:
        await run_input()

    assert exc.value.status is None
    assert exc.value.permanent is False
    assert str(exc.value) == "SRS unreachable: ConnectError"


@respx.mock
async def test_malformed_json_is_no_verdict():
    respx.post(INFERENCE).mock(return_value=httpx.Response(200, text="not json"))

    with pytest.raises(SrsUnavailable, match="malformed JSON"):
        await run_input()


@respx.mock
async def test_nothing_sensitive_is_logged(caplog):
    respx.post(TOOL_CALL).mock(return_value=httpx.Response(500, text="body echo"))

    with caplog.at_level("DEBUG", logger="computeragent.policy.srs"), pytest.raises(SrsUnavailable):
        await run_tool_call(tool_args={"command": "cat /secret/file"})

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert KEY not in logged
    assert "/secret/file" not in logged
    assert "body echo" not in logged


# ── helpers ──────────────────────────────────────────────────────────────────


def test_text_blob_keeps_top_level_strings_in_order():
    assert tool_args_text_blob({"a": "one", "n": 3, "b": "", "c": "two", "d": {"x": "y"}}) == (
        "one\ntwo"
    )
    assert tool_args_text_blob(None) == ""
    assert tool_args_text_blob({}) == ""


@pytest.mark.parametrize(
    ("tool", "engine", "expected"),
    [
        ("Bash", "claude-agent-sdk", "claude-agent-sdk__Bash"),
        ("NotebookEdit", "claude-agent-sdk", "claude-agent-sdk__NotebookEdit"),
        ("cli", "gitagent", "gitagent__cli"),
        ("write", "gitagent", "gitagent__write"),
        ("task_tracker", "gitagent", "gitagent__task_tracker"),
        ("foo__bar", "gitagent", "gitagent__foo__bar"),
        ("mcp__probe__get_build_info", "claude-agent-sdk", "mcp__probe__get_build_info"),
        ("probe__get_build_info", "gitagent", "mcp__probe__get_build_info"),
        ("mcp__orchestration__dispatch_many", "gitagent", "mcp__orchestration__dispatch_many"),
    ],
)
def test_policy_tool_name(tool, engine, expected):
    assert policy_tool_name(tool, engine_name=engine, mcp_server_names=["probe"]) == expected


def test_the_two_engines_never_share_a_builtin_name():
    assert policy_tool_name("write", engine_name="gitagent", mcp_server_names=[]) != (
        policy_tool_name("Write", engine_name="claude-agent-sdk", mcp_server_names=[])
    )


def test_mcp_server_names_of_tolerates_any_shape():
    assert mcp_server_names_of({"mcp_servers": {"jira": {}, "gh": {}}}) == ["jira", "gh"]
    assert mcp_server_names_of({"mcp_servers": []}) == []
    assert mcp_server_names_of(None) == []
    assert mcp_server_names_of("x") == []
