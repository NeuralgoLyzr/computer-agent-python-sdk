"""The user's message through SRS ``llm_input`` before the turn.

Ported from CAS harness-server ``session.ts`` (redactUserMessage /
aggregateRedaction) and the redactInput cases of ``srs-policy-decider.test.ts``.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from computeragent.policy.srs_gate import TurnPolicy
from computeragent.policy.srs_input import (
    DEFAULT_BLOCK_REPLY,
    StreamingInputNotCheckable,
    check_user_message,
)

SRS = "http://egress.internal/srs"
INFERENCE = f"{SRS}/v1/rai/inference"
KEY = "sbx_key"
TURN = TurnPolicy(policy_ids=("org1", "local1"), fail_open=None, timeout_s=5.0)


async def check(message, turn=TURN):
    return await check_user_message(
        message, turn, srs_url=SRS, api_key=KEY, principal_id="my-agent"
    )


def masking_srs():
    """Echoes each text back with emails masked and blocks on 'BLOCKME'."""

    def handler(request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content)["input_text"]
        if "BLOCKME" in text:
            return httpx.Response(
                200,
                json={"blocked": True, "block_reason": "nope", "blocked_by_policy_id": "local1"},
            )
        masked = text.replace("a@b.com", "<EMAIL_ADDRESS_1>")
        return httpx.Response(
            200,
            json={
                "processed_text": masked,
                "redaction_map": {"a@b.com": "<EMAIL_ADDRESS_1>"} if masked != text else {},
            },
        )

    return respx.post(INFERENCE).mock(side_effect=handler)


@respx.mock
async def test_a_string_message_is_masked():
    route = masking_srs()

    decision = await check("mail a@b.com")

    assert decision.message == "mail <EMAIL_ADDRESS_1>"
    assert decision.outcome == "redacted"
    assert decision.blocked is False
    assert decision.entity_counts == {"EMAIL_ADDRESS": 1}
    assert decision.checked is True
    sent = json.loads(route.calls.last.request.content)
    assert sent["policy_ids"] == ["org1", "local1"]
    assert sent["stage"] == "llm_input"
    assert sent["principal_id"] == "my-agent"


@respx.mock
async def test_every_text_block_is_checked_and_other_blocks_kept():
    route = masking_srs()
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
    assert decision.message["content"][0]["text"] == "mail <EMAIL_ADDRESS_1>"
    assert decision.message["content"][1] == {"type": "image", "source": {"data": "..."}}
    assert decision.message["content"][2]["text"] == "and <EMAIL_ADDRESS_1> again"
    assert decision.entity_counts == {"EMAIL_ADDRESS": 2}
    # The caller's own message is not modified.
    assert message["content"][0]["text"] == "mail a@b.com"


@respx.mock
async def test_an_sdk_shaped_user_message_is_checked():
    masking_srs()
    message = {"type": "user", "message": {"role": "user", "content": "mail a@b.com"}}

    decision = await check(message)

    assert decision.message["message"]["content"] == "mail <EMAIL_ADDRESS_1>"


@respx.mock
async def test_a_list_of_messages_is_checked():
    masking_srs()

    decision = await check(
        [{"role": "user", "content": "a@b.com"}, {"role": "user", "content": "hi"}]
    )

    assert decision.message[0]["content"] == "<EMAIL_ADDRESS_1>"
    assert decision.message[1]["content"] == "hi"


@respx.mock
async def test_one_blocked_block_blocks_the_message():
    masking_srs()

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
    respx.post(INFERENCE).mock(return_value=httpx.Response(200, json={"blocked": True}))

    decision = await check("x")

    assert decision.reply == DEFAULT_BLOCK_REPLY


@respx.mock
async def test_a_clean_message_is_unchanged():
    masking_srs()

    decision = await check("hello")

    assert decision.message == "hello"
    assert decision.outcome == "clean"


@respx.mock
async def test_no_policies_makes_no_call():
    route = masking_srs()

    decision = await check("a@b.com", TurnPolicy(policy_ids=(), fail_open=None, timeout_s=5))

    assert decision.message == "a@b.com"
    assert decision.checked is False
    assert route.call_count == 0


@respx.mock
async def test_empty_text_makes_no_call():
    route = masking_srs()

    decision = await check({"content": [{"type": "image"}, {"type": "text", "text": ""}]})

    assert decision.outcome == "clean"
    assert route.call_count == 0


@respx.mock
async def test_an_unresolved_turn_blocks_without_a_call():
    route = masking_srs()

    decision = await check("hello", TurnPolicy.unresolvable())

    assert decision.blocked is True
    assert decision.reply == (
        "[Content blocked: RAI policy check failed: org RAI policy could not be loaded]"
    )
    assert route.call_count == 0


@respx.mock
async def test_no_verdict_blocks_when_an_org_policy_is_fail_closed():
    respx.post(INFERENCE).mock(return_value=httpx.Response(503))

    decision = await check("hello", TurnPolicy(policy_ids=("o1",), fail_open=False, timeout_s=5))

    assert decision.blocked is True
    assert decision.outcome == "error"
    assert decision.reply == "[Content blocked: RAI policy check failed: SRS 503]"


@pytest.mark.parametrize("fail_open", [True, None])
@respx.mock
async def test_no_verdict_passes_unchanged_otherwise(fail_open):
    """Every org policy fail-open, or only the agent's own policies (whose
    lyzr-agent default is fail-open)."""
    respx.post(INFERENCE).mock(side_effect=httpx.ConnectError("down"))

    decision = await check(
        "a@b.com", TurnPolicy(policy_ids=("o1",), fail_open=fail_open, timeout_s=5)
    )

    assert decision.blocked is False
    assert decision.outcome == "error"
    assert decision.message == "a@b.com"


@respx.mock
async def test_the_worst_outcome_wins():
    """error > blocked > redacted > clean."""
    calls = iter(
        [
            httpx.Response(200, json={"processed_text": "<X_1>", "redaction_map": {"v": "<X_1>"}}),
            httpx.Response(500),
        ]
    )
    respx.post(INFERENCE).mock(side_effect=lambda request: next(calls))

    decision = await check(
        {"content": [{"type": "text", "text": "v"}, {"type": "text", "text": "w"}]},
        TurnPolicy(policy_ids=("o1",), fail_open=True, timeout_s=5),
    )

    assert decision.outcome == "error"
    assert decision.message["content"][0]["text"] == "<X_1>"
    assert decision.message["content"][1]["text"] == "w"


async def test_streaming_input_is_refused_while_policies_apply():
    async def stream():
        yield {"role": "user", "content": "hi"}

    with pytest.raises(StreamingInputNotCheckable):
        await check(stream())


async def test_streaming_input_is_fine_with_no_policies():
    async def stream():
        yield {"role": "user", "content": "hi"}

    decision = await check(stream(), TurnPolicy(policy_ids=(), fail_open=None, timeout_s=5))
    assert decision.outcome == "clean"
