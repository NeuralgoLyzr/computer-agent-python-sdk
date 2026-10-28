"""Guardrails through a ComputerAgent run.

The user's message and every tool call go to ``{guardrails_url}/v1/check``
through the egress proxy; the gateway applies the org's and the agent's
policies. The SDK reads no policies and sends no policy ids.
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
GUARDRAILS = "http://egress.internal/guardrails"
CHECK = f"{GUARDRAILS}/v1/check"


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


def gateway(tool_decision="allow", **tool_data):
    """Masks emails, blocks on 'BLOCKME', answers tool calls with ``tool_decision``."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["op"] == "tool-input":
            data = {"decision": tool_decision, "op": "tool-input", **tool_data}
        elif "BLOCKME" in body["subject"]["text"]:
            data = {"decision": "block", "blockReason": "nope", "blockedByPolicyId": "local1"}
        elif "a@b.com" in body["subject"]["text"]:
            data = {
                "decision": "rewrite",
                "text": body["subject"]["text"].replace("a@b.com", "[EMAIL]"),
            }
        else:
            data = {"decision": "allow"}
        return httpx.Response(200, json={"data": data})

    return respx.post(CHECK).mock(side_effect=handler)


def build(agent_doc, **kwargs) -> ComputerAgent:
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))
    params: dict = {
        "agent_id": AGENT_ID,
        "base_url": BASE,
        "api_key": KEY,
        "engine": RecordingEngine(),
        "llm_url": LLM_URL,
        "mcp_url": MCP_URL,
        "guardrails_url": GUARDRAILS,
    }
    params.update(kwargs)
    return ComputerAgent(**params)


def test_the_guardrails_url_is_required():
    with pytest.raises(SettingsError, match="guardrails_url"):
        ComputerAgent(
            agent_id=AGENT_ID,
            base_url=BASE,
            api_key=KEY,
            engine="gitagent",
            llm_url=LLM_URL,
            mcp_url=MCP_URL,
        )


@respx.mock
async def test_the_message_is_checked_and_masked_before_the_engine(agent_doc):
    route = gateway()
    engine = RecordingEngine()

    async with build(agent_doc, engine=engine) as agent:
        await agent.chat("mail a@b.com")

    assert json.loads(route.calls.last.request.content) == {
        "op": "input",
        "subject": {"text": "mail a@b.com"},
    }
    assert route.calls.last.request.headers["x-api-key"] == KEY
    assert engine.messages[0]["content"] == "mail [EMAIL]"


@respx.mock
async def test_no_org_policy_route_is_called(agent_doc):
    gateway()
    agent = build(agent_doc)
    others = respx.route().mock(return_value=httpx.Response(599))

    async with agent:
        await agent.chat("hello")

    # Only the agent document and the check were requested.
    assert not others.called


@respx.mock
async def test_every_turn_is_checked(agent_doc):
    route = gateway()

    async with build(agent_doc) as agent:
        await agent.chat("one")
        await agent.chat("two")

    assert route.call_count == 2


@respx.mock
async def test_a_blocked_message_never_starts_the_engine(agent_doc, stub_sink):
    gateway()
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
    assert decisions[0].payload["engine"] == "guardrails"
    assert decisions[0].payload["stage"] == "llm_input"
    assert decisions[0].payload["outcome"] == "blocked"
    assert decisions[0].payload["policy_id"] == "local1"
    assert "BLOCKME" not in json.dumps(decisions[0].payload)


@respx.mock
async def test_the_turn_after_a_block_runs(agent_doc):
    gateway()
    engine = RecordingEngine()

    async with build(agent_doc, engine=engine) as agent:
        await agent.chat("BLOCKME")
        await agent.chat("fine")

    assert engine.starts == 1
    assert engine.messages[0]["content"] == "fine"


@respx.mock
async def test_no_verdict_blocks_the_turn(agent_doc):
    respx.post(CHECK).mock(
        return_value=httpx.Response(502, json={"error": {"code": "rai_unavailable"}})
    )
    engine = RecordingEngine()

    async with build(agent_doc, engine=engine) as agent:
        result = await agent.chat("hello")

    assert engine.starts == 0
    assert result.messages[0].result == (
        "[Content blocked: RAI policy check failed: guardrails 502 (rai_unavailable)]"
    )


@respx.mock
async def test_tool_calls_are_checked_with_the_engines_id(agent_doc):
    route = gateway(
        "block", results=[{"decision": "block", "deniedBy": "cedar", "reason": "no rm"}]
    )

    async with build(agent_doc) as agent:
        decider = agent._coordinator._policy_decider  # type: ignore[attr-defined]
        result = await decider(
            "Bash", {"command": "rm -rf /"}, {"session_id": "s1", "tool_use_id": "toolu_7"}
        )

    assert isinstance(result, PermissionResultDeny)
    assert result.message == "no rm"
    (call,) = json.loads(route.calls.last.request.content)["subject"]["toolCalls"]
    assert call == {
        "id": "toolu_7",
        "name": "claude-agent-sdk__Bash",
        "arguments": {"command": "rm -rf /"},
    }


@respx.mock
async def test_a_tool_call_without_an_id_is_denied(agent_doc):
    route = gateway()
    agent = build(agent_doc)
    decider = agent._coordinator._policy_decider  # type: ignore[attr-defined]

    result = await decider("Bash", {"command": "ls"}, {"session_id": "s1"})

    assert isinstance(result, PermissionResultDeny)
    assert route.call_count == 0


@respx.mock
async def test_a_callers_policy_decider_replaces_the_tool_gate_but_not_the_input_check(
    agent_doc,
):
    route = gateway()

    async def mine(tool_name, tool_input, context):
        return PermissionResultAllow(updated_input=tool_input)

    async with build(agent_doc, policy_decider=mine) as agent:
        await agent.chat("hello")
        assert agent._coordinator._policy_decider is mine  # type: ignore[attr-defined]

    assert route.call_count == 1


@respx.mock
async def test_a_streamed_message_is_checked_before_the_engine_sees_it(agent_doc):
    route = gateway()
    engine = RecordingEngine()

    async def stream():
        yield {"role": "user", "content": "mail a@b.com"}

    async with build(agent_doc, engine=engine) as agent:
        await agent.chat(stream())

    assert route.call_count == 1
    assert engine.messages[0]["content"] == "mail [EMAIL]"


@respx.mock
async def test_a_blocked_streamed_message_never_starts_the_engine(agent_doc):
    gateway()
    engine = RecordingEngine()

    async def stream():
        yield {"role": "user", "content": "fine"}
        yield {"role": "user", "content": "BLOCKME"}

    async with build(agent_doc, engine=engine) as agent:
        result = await agent.chat(stream())

    assert engine.starts == 0
    assert result.messages[0].result == "nope"
