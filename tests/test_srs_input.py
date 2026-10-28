"""The user's message through the guardrails ``input`` check before the turn.

Folding ported from CAS harness-server ``session.ts`` (redactUserMessage /
aggregateRedaction); the gateway owns policy selection and fail-open.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from computeragent.policy.srs_input import DEFAULT_BLOCK_REPLY, check_user_message

GUARDRAILS = "http://egress.internal/guardrails"
CHECK = f"{GUARDRAILS}/v1/check"
KEY = "sbt_key"


async def check(message):
    return await check_user_message(message, guardrails_url=GUARDRAILS, api_key=KEY)


def data(**fields):
    return httpx.Response(200, json={"data": {"op": "input", **fields}})


def masking_gateway():
    """Masks emails with a rewrite and blocks on 'BLOCKME'."""

    def handler(request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content)["subject"]["text"]
        if "BLOCKME" in text:
            return data(decision="block", blockReason="nope", blockedByPolicyId="local1")
        if "a@b.com" in text:
            return data(decision="rewrite", text=text.replace("a@b.com", "[EMAIL]"))
        return data(decision="allow")

    return respx.post(CHECK).mock(side_effect=handler)


@respx.mock
async def test_a_string_message_is_masked():
    route = masking_gateway()

    decision = await check("mail a@b.com")

    assert decision.message == "mail [EMAIL]"
    assert decision.outcome == "redacted"
    assert decision.blocked is False
    assert decision.checked is True
    assert json.loads(route.calls.last.request.content) == {
        "op": "input",
        "subject": {"text": "mail a@b.com"},
    }
    assert route.calls.last.request.headers["x-api-key"] == KEY


@respx.mock
async def test_every_text_block_is_checked_and_other_blocks_kept():
    route = masking_gateway()
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "mail a@b.com"},
            {"type": "image", "source": {"data": "..."}},
            {"type": "text", "text": "and a@b.com again"},
        ],
    }

    decision = await check(message)

    assert route.call_count == 2
    assert decision.message["content"][0]["text"] == "mail [EMAIL]"
    assert decision.message["content"][1] == {"type": "image", "source": {"data": "..."}}
    assert decision.message["content"][2]["text"] == "and [EMAIL] again"
    # The caller's own message is not modified.
    assert message["content"][0]["text"] == "mail a@b.com"


@respx.mock
async def test_an_sdk_shaped_user_message_is_checked():
    masking_gateway()
    message = {"type": "user", "message": {"role": "user", "content": "mail a@b.com"}}

    decision = await check(message)

    assert decision.message["message"]["content"] == "mail [EMAIL]"


@respx.mock
async def test_a_list_of_messages_is_checked():
    masking_gateway()

    decision = await check(
        [{"role": "user", "content": "a@b.com"}, {"role": "user", "content": "hi"}]
    )

    assert decision.message[0]["content"] == "[EMAIL]"
    assert decision.message[1]["content"] == "hi"


@respx.mock
async def test_one_blocked_block_blocks_the_message():
    masking_gateway()

    decision = await check(
        {"content": [{"type": "text", "text": "fine"}, {"type": "text", "text": "BLOCKME"}]}
    )

    assert decision.blocked is True
    assert decision.outcome == "blocked"
    assert decision.reason == "nope"
    assert decision.reply == "nope"
    assert decision.policy_id == "local1"


@respx.mock
async def test_a_block_with_no_reason_has_the_default_reply():
    respx.post(CHECK).mock(return_value=data(decision="block"))

    decision = await check("x")

    assert decision.reply == DEFAULT_BLOCK_REPLY


@respx.mock
async def test_a_clean_message_is_unchanged():
    masking_gateway()

    decision = await check("hello")

    assert decision.message == "hello"
    assert decision.outcome == "clean"


@respx.mock
async def test_empty_text_makes_no_call():
    route = masking_gateway()

    decision = await check({"content": [{"type": "image"}, {"type": "text", "text": ""}]})

    assert decision.outcome == "clean"
    assert decision.checked is False
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            httpx.Response(502, json={"error": {"code": "rai_unavailable"}}),
            "guardrails 502 (rai_unavailable)",
        ),
        (httpx.Response(413), "guardrails 413"),
        (httpx.Response(400), "guardrails 400"),
    ],
)
@respx.mock
async def test_no_verdict_blocks(response, reason):
    respx.post(CHECK).mock(return_value=response)

    decision = await check("a@b.com")

    assert decision.blocked is True
    assert decision.outcome == "error"
    assert decision.reply == f"[Content blocked: RAI policy check failed: {reason}]"


@respx.mock
async def test_a_timeout_blocks():
    respx.post(CHECK).mock(side_effect=httpx.ReadTimeout("slow"))

    decision = await check("hello")

    assert decision.blocked is True
    assert decision.reply == (
        "[Content blocked: RAI policy check failed: guardrails unreachable: ReadTimeout]"
    )


@respx.mock
async def test_the_worst_outcome_wins():
    """error > blocked > redacted > clean."""
    calls = iter([data(decision="rewrite", text="[X]"), httpx.Response(500)])
    respx.post(CHECK).mock(side_effect=lambda request: next(calls))

    decision = await check(
        {"content": [{"type": "text", "text": "v"}, {"type": "text", "text": "w"}]}
    )

    assert decision.outcome == "error"
    assert decision.blocked is True
    assert decision.message["content"][0]["text"] == "[X]"
    assert decision.message["content"][1]["text"] == "w"


@respx.mock
async def test_a_streamed_message_is_checked_entry_by_entry():
    route = masking_gateway()

    async def stream():
        yield {"role": "user", "content": "mail a@b.com"}
        yield "and a@b.com"

    decision = await check(stream())

    assert route.call_count == 2
    assert decision.message == [
        {"role": "user", "content": "mail [EMAIL]"},
        {"role": "user", "content": "and [EMAIL]"},
    ]
    assert decision.outcome == "redacted"


@respx.mock
async def test_one_blocked_entry_in_a_stream_blocks_the_turn():
    masking_gateway()

    async def stream():
        yield {"role": "user", "content": "fine"}
        yield {"role": "user", "content": "BLOCKME"}

    decision = await check(stream())

    assert decision.blocked is True
    assert decision.reply == "nope"


@respx.mock
async def test_bare_string_entries_in_a_list_are_checked():
    route = masking_gateway()

    decision = await check(["a@b.com", {"role": "user", "content": "hi"}])

    assert route.call_count == 2
    assert decision.message[0] == {"role": "user", "content": "[EMAIL]"}
