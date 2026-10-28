"""Shared pieces for the app-server tests: a fake engine and an app over a real
ComputerAgent, with the agent document and the guardrails check mocked."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import respx
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

from computeragent.harness import ComputerAgent
from computeragent.protocol_types import (
    EngineCapabilities,
    EngineContext,
    EngineEvent,
    PermissionRequest,
)
from computeragent.server import create_app

AGENT_ID = "6aa93f1b3817a13d3103d2c1"
BASE = "http://egress.internal/computer-agent-api/agentos/api"
GUARDRAILS = "http://egress.internal/guardrails"


class FakeEngine:
    """Answers each user message with one assistant text and a result, then a
    cumulative usage snapshot, as the Claude engine does. ``ask_permission``
    makes it request approval for one tool first; ``fail`` makes it raise."""

    name = "fake"
    capabilities = EngineCapabilities(
        streaming_input=True,
        partial_messages=False,
        permission_callback=True,
        sessions=True,
        budget=False,
    )

    def __init__(
        self, *, ask_permission: bool = False, fail: bool = False, hold: Any = None
    ) -> None:
        self.ask_permission = ask_permission
        self.fail = fail
        self.hold = hold  # an asyncio.Event the turn waits on
        self.contexts: list[EngineContext] = []
        self.messages: list[Any] = []
        self.decisions: list[Any] = []

    async def start_session(self, ctx: EngineContext) -> AsyncIterator[EngineEvent]:
        self.contexts.append(ctx)
        async for message in ctx.user_message_queue:
            self.messages.append(message)
            break
        if self.fail:
            raise RuntimeError("engine exploded")
        if self.hold is not None:
            await self.hold.wait()
        if self.ask_permission:
            decision = await ctx.on_permission_request(
                PermissionRequest(call_id="call_1", tool_name="Write", input={"path": "a.txt"})
            )
            self.decisions.append(decision)
        yield EngineEvent(
            kind="sdk_message",
            payload=AssistantMessage(
                content=[TextBlock(text="hello back")], model="claude-sonnet-4-5"
            ),
        )
        yield EngineEvent(
            kind="sdk_message",
            payload=ResultMessage(
                subtype="success",
                duration_ms=5,
                duration_api_ms=4,
                is_error=False,
                num_turns=1,
                session_id="engine-sess",
                total_cost_usd=0.01,
                result="hello back",
            ),
        )
        yield EngineEvent(
            kind="usage_snapshot",
            input_tokens=10,
            output_tokens=3,
            cost_usd=0.01,
            cost_semantic="cumulative",
        )


def mock_backends(agent_doc: dict[str, Any]) -> None:
    """The agent document, and a guardrails check that masks emails and blocks 'BLOCKME'."""
    respx.get(f"{BASE}/v1/agents/{AGENT_ID}").mock(return_value=httpx.Response(200, json=agent_doc))

    def guardrails(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["op"] != "input":
            return httpx.Response(200, json={"data": {"decision": "allow"}})
        text = body["subject"]["text"]
        if "BLOCKME" in text:
            data = {
                "decision": "block",
                "blockReason": "blocked by policy",
                "blockedByPolicyId": "pol_1",
            }
        elif "@" in text:
            data = {"decision": "rewrite", "text": text.replace("a@b.com", "[EMAIL]")}
        else:
            data = {"decision": "allow"}
        return httpx.Response(200, json={"data": data})

    respx.post(f"{GUARDRAILS}/v1/check").mock(side_effect=guardrails)


def make_app(engine: FakeEngine, calls: list[dict[str, Any]] | None = None) -> Any:
    async def no_opinion(*_a: Any) -> None:  # lets approvals reach the client
        return None

    def factory(**kwargs: Any) -> ComputerAgent:
        if calls is not None:
            calls.append(dict(kwargs))
        return ComputerAgent(
            agent_id=AGENT_ID,
            base_url=BASE,
            engine=engine,
            llm_url="http://egress.internal/llm",
            mcp_url="http://egress.internal/mcp/v1",
            guardrails_url=GUARDRAILS,
            policy_decider=no_opinion if engine.ask_permission else None,
            **kwargs,
        )

    return create_app(agent_factory=factory)


def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://image")


def session_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "engine": "claude-agent-sdk",
        "identity": {"loader": "gitagentprotocol", "source": {"type": "local", "path": "/agent"}},
        "envs": {"LYZR_API_KEY": "sbt_1", "OTHER": "x"},
        "sessionId": "s1",
        "streamingInput": True,
    }
    body.update(overrides)
    return body


def frames(text: str) -> list[tuple[int | None, str, dict[str, Any]]]:
    """(id, event name, data) per SSE frame; comments skipped."""
    out = []
    for block in text.split("\n\n"):
        lines = [line for line in block.splitlines() if line and not line.startswith(":")]
        if not lines:
            continue
        fields = dict(line.split(": ", 1) for line in lines)
        out.append(
            (
                int(fields["id"]) if "id" in fields else None,
                fields["event"],
                json.loads(fields["data"]),
            )
        )
    return out


def env_key() -> str | None:
    return os.environ.get("LYZR_API_KEY")
