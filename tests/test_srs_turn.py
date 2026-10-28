"""SRS guardrails through a ComputerAgent run.

Every turn combines the org (global) policies from ``agent_api_url`` with the
agent's own (local) ``rai_policy_id`` from the agent document; the user's
message and every tool call are checked against that one set.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny, ResultMessage

from computeragent.harness import ComputerAgent
from computeragent.harness.settings import SettingsError
from computeragent.protocol_types import EngineCapabilities, EngineContext, EngineEvent
from computeragent.telemetry.pipeline import Pipeline

AGENT_ID = "6aa93f1b3817a13d3103d2c1"
BASE = "http://localhost:8788/agentos/api"
URL = f"{BASE}/v1/agents/{AGENT_ID}"
KEY = "sbx_key"
LLM_URL = "http://egress.internal/llm"
MCP_URL = "http://egress.internal/mcp"
AGENT_API = "http://egress.internal/agent-api"
ORG_URL = f"{AGENT_API}/v3/org/rai-policy"
SRS = "http://egress.internal/srs"
INFERENCE = f"{SRS}/v1/rai/inference"
TOOL_CALL = f"{SRS}/v1/guardrails/evaluate-tool-call"


class RecordingEngine:
    name = "claude-agent-sdk"
    capabilities = EngineCapabilities(
        streaming_input=True,
        partial_messages=False,
        permission_callback=True,
        sessions=True,
        budget=False,
    )

    def __init__(self) -> None:
        self.messages: list = []
        self.starts = 0

    async def start_session(self, ctx: EngineContext) -> AsyncIterator[EngineEvent]:
        self.starts += 1
        async for message in ctx.user_message_queue:
            self.messages.append(message)
            break
        yield EngineEvent(
            kind="sdk_message", payload={"type": "assistant", "message": {"content": "ok"}}
        )


def org_policies(*rows):
    return respx.get(ORG_URL).mock(return_value=httpx.Response(200, json={"policies": list(rows)}))


def masking_srs():
    def handler(request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content)["input_text"]
        if "BLOCKME" in text:
            return httpx.Response(
                200,
                json={"blocked": True, "block_reason": "nope", "blocked_by_policy_id": "local1"},
            )
        return httpx.Response(200, json={"processed_text": text.replace("a@b.com", "<EMAIL_1>")})

    return respx.post(INFERENCE).mock(side_effect=handler)


def build(agent_doc, *, rai_policy_id="local1", **kwargs) -> ComputerAgent:
    agent_doc["rai_policy_id"] = rai_policy_id
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))
    params: dict = {
        "agent_id": AGENT_ID,
        "base_url": BASE,
        "api_key": KEY,
        "engine": RecordingEngine(),
        "llm_url": LLM_URL,
        "mcp_url": MCP_URL,
        "agent_api_url": AGENT_API,
        "srs_url": SRS,
    }
    params.update(kwargs)
    return ComputerAgent(**params)


@pytest.mark.parametrize("missing", ["agent_api_url", "srs_url"])
def test_the_guardrail_urls_are_required(missing):
    params = {
        "agent_id": AGENT_ID,
        "base_url": BASE,
        "api_key": KEY,
        "engine": "gitagent",
        "llm_url": LLM_URL,
        "mcp_url": MCP_URL,
        "agent_api_url": AGENT_API,
        "srs_url": SRS,
    }
    del params[missing]

    with pytest.raises(SettingsError, match=missing):
        ComputerAgent(**params)


@respx.mock
def test_the_local_policy_comes_from_the_agent_document(agent_doc):
    agent = build(agent_doc, rai_policy_id=" local1 ")

    assert agent.agent.rai_policy_id == "local1"


@respx.mock
async def test_the_input_check_uses_org_then_local_policies(agent_doc):
    org = org_policies(
        {"policy_id": "org1", "enabled": True}, {"policy_id": "org2", "enabled": True}
    )
    srs = masking_srs()
    engine = RecordingEngine()

    async with build(agent_doc, engine=engine) as agent:
        await agent.chat("mail a@b.com")

    assert org.calls.last.request.headers["x-api-key"] == KEY
    sent = json.loads(srs.calls.last.request.content)
    assert sent["policy_ids"] == ["org1", "org2", "local1"]
    assert sent["stage"] == "llm_input"
    assert sent["principal_id"] == agent_doc["name"]
    assert engine.messages[0]["content"] == "mail <EMAIL_1>"


@respx.mock
async def test_org_policies_are_read_again_every_turn(agent_doc):
    org = org_policies()
    masking_srs()

    async with build(agent_doc) as agent:
        await agent.chat("one")
        await agent.chat("two")

    assert org.call_count == 2


@respx.mock
async def test_a_blocked_message_never_starts_the_engine(agent_doc, stub_sink):
    org_policies()
    masking_srs()
    engine = RecordingEngine()

    async with build(
        agent_doc, engine=engine, telemetry_pipeline=Pipeline(sinks=[stub_sink])
    ) as agent:
        result = await agent.chat("BLOCKME please")

    assert engine.starts == 0
    (final,) = result.messages
    assert isinstance(final, ResultMessage)
    assert final.result == "nope"
    assert final.is_error is False
    decisions = [e for e in stub_sink.events if e.kind == "policy_decision"]
    assert len(decisions) == 1
    assert decisions[0].payload["stage"] == "llm_input"
    assert decisions[0].payload["outcome"] == "blocked"
    assert decisions[0].payload["policy_id"] == "local1"
    assert "BLOCKME" not in json.dumps(decisions[0].payload)


@respx.mock
async def test_the_turn_after_a_block_runs(agent_doc):
    org_policies()
    masking_srs()
    engine = RecordingEngine()

    async with build(agent_doc, engine=engine) as agent:
        await agent.chat("BLOCKME")
        await agent.chat("fine")

    assert engine.starts == 1
    assert engine.messages[0]["content"] == "fine"


@respx.mock
async def test_unreadable_org_policies_block_the_turn(agent_doc):
    respx.get(ORG_URL).mock(return_value=httpx.Response(500))
    srs = masking_srs()
    engine = RecordingEngine()

    async with build(agent_doc, engine=engine) as agent:
        result = await agent.chat("hello")

    assert engine.starts == 0
    assert result.messages[0].result == (
        "[Content blocked: RAI policy check failed: org RAI policy could not be loaded]"
    )
    assert srs.call_count == 0


@respx.mock
async def test_no_policies_anywhere_makes_no_srs_call(agent_doc):
    org_policies()
    srs = masking_srs()
    engine = RecordingEngine()

    async with build(agent_doc, engine=engine, rai_policy_id=None) as agent:
        await agent.chat("mail a@b.com")

    assert srs.call_count == 0
    assert engine.messages[0]["content"] == "mail a@b.com"


@respx.mock
async def test_tool_calls_are_checked_against_the_same_combined_set(agent_doc):
    org_policies({"policy_id": "org1", "enabled": True})
    masking_srs()
    tool_call = respx.post(TOOL_CALL).mock(
        return_value=httpx.Response(
            200, json={"allowed": False, "denied_by": "cedar", "reason": "no rm"}
        )
    )

    async with build(agent_doc) as agent:
        await agent.chat("hello")
        decider = agent._coordinator._policy_decider  # type: ignore[attr-defined]
        result = await decider("Bash", {"command": "rm -rf /"}, {"session_id": "s1"})

    assert isinstance(result, PermissionResultDeny)
    assert result.message == "no rm"
    sent = json.loads(tool_call.calls.last.request.content)
    assert sent["policy_ids"] == ["org1", "local1"]
    assert sent["tool_name"] == "claude-agent-sdk__Bash"
    assert sent["principal_id"] == agent_doc["name"]


@respx.mock
async def test_a_tool_call_before_any_turn_is_denied(agent_doc):
    agent = build(agent_doc)
    decider = agent._coordinator._policy_decider  # type: ignore[attr-defined]

    result = await decider("Bash", {"command": "ls"}, {"session_id": "s1"})

    assert isinstance(result, PermissionResultDeny)


@respx.mock
async def test_a_callers_policy_decider_replaces_the_tool_gate_but_not_the_input_check(
    agent_doc,
):
    org_policies()
    srs = masking_srs()

    async def mine(tool_name, tool_input, context):
        return PermissionResultAllow(updated_input=tool_input)

    async with build(agent_doc, policy_decider=mine) as agent:
        await agent.chat("hello")
        assert agent._coordinator._policy_decider is mine  # type: ignore[attr-defined]

    assert srs.call_count == 1


@respx.mock
async def test_a_streamed_message_is_checked_before_the_engine_sees_it(agent_doc):
    org_policies()
    srs = masking_srs()
    engine = RecordingEngine()

    async def stream():
        yield {"role": "user", "content": "mail a@b.com"}

    async with build(agent_doc, engine=engine) as agent:
        await agent.chat(stream())

    assert srs.call_count == 1
    assert engine.messages[0]["content"] == "mail <EMAIL_1>"


@respx.mock
async def test_a_blocked_streamed_message_never_starts_the_engine(agent_doc):
    org_policies()
    masking_srs()
    engine = RecordingEngine()

    async def stream():
        yield {"role": "user", "content": "fine"}
        yield {"role": "user", "content": "BLOCKME"}

    async with build(agent_doc, engine=engine) as agent:
        result = await agent.chat(stream())

    assert engine.starts == 0
    assert result.messages[0].result == "nope"
