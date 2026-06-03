"""Coverage for the Anthropic backend of ``GitAgentEngine``.

The engine dispatches to Anthropic when the model name starts with
``anthropic:`` or bare ``claude-*``. These tests mock
``_build_anthropic_client`` so they exercise the loop, message
construction, tool routing, and usage emission without burning real
Anthropic credits.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from computeragent.engines.git_agent import GitAgentEngine, _select_backend
from computeragent.protocol_types import (
    AgentManifest,
    EngineContext,
    EngineEvent,
    PermissionRequest,
)


def _text_block(text: str) -> Any:
    class _Block:
        def __init__(self) -> None:
            self.type = "text"
            self.text = text

    return _Block()


def _tool_use_block(call_id: str, name: str, inputs: dict[str, Any]) -> Any:
    class _Block:
        def __init__(self) -> None:
            self.type = "tool_use"
            self.id = call_id
            self.name = name
            self.input = inputs

    return _Block()


def _anthropic_response(
    *,
    content_blocks: list[Any],
    stop_reason: str = "end_turn",
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> Any:
    class _Usage:
        def __init__(self) -> None:
            self.input_tokens = input_tokens
            self.output_tokens = output_tokens
            self.cache_creation_input_tokens = 0
            self.cache_read_input_tokens = 0

    class _Resp:
        def __init__(self) -> None:
            self.content = content_blocks
            self.stop_reason = stop_reason
            self.usage = _Usage()
            self.model = "claude-sonnet-4-5"
            self.role = "assistant"

    return _Resp()


async def _no_perm(_req: PermissionRequest) -> dict[str, Any]:
    return {"behavior": "allow", "updated_input": {}}


def _ctx(
    *,
    user_messages: list[dict[str, Any]],
    options: dict[str, Any],
    on_perm=_no_perm,
) -> EngineContext:
    async def _queue():
        for msg in user_messages:
            yield msg

    return EngineContext(
        session_id="sess_test",
        manifest=AgentManifest(name="git-agent-anthropic-test", options=options),
        workspace_path=Path("/tmp"),
        envs={},
        user_message_queue=_queue(),
        on_permission_request=on_perm,
        cancel_event=asyncio.Event(),
    )


def test_select_backend_anthropic_prefix():
    assert _select_backend("anthropic:claude-sonnet-4-5") == "anthropic"
    assert _select_backend("ANTHROPIC:claude-haiku") == "anthropic"


def test_select_backend_bare_claude():
    assert _select_backend("claude-sonnet-4-5-20250929") == "anthropic"


def test_select_backend_openai_default():
    assert _select_backend("gpt-4o-mini") == "openai"
    assert _select_backend("openai:gpt-4o") == "openai"
    # Unknown model — fall back to OpenAI for backwards compat with the
    # original gitclaw-shaped path.
    assert _select_backend("some-other-model") == "openai"


@pytest.mark.asyncio
async def test_anthropic_backend_single_turn(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    fake_client = AsyncMock()
    fake_client.messages.create = AsyncMock(
        return_value=_anthropic_response(
            content_blocks=[_text_block("Hello there.")],
            stop_reason="end_turn",
            input_tokens=12,
            output_tokens=3,
        )
    )
    monkeypatch.setattr(
        "computeragent.engines.git_agent._build_anthropic_client",
        lambda: fake_client,
    )

    engine = GitAgentEngine()
    ctx = _ctx(
        user_messages=[{"role": "user", "content": "say hi"}],
        options={
            "model": "anthropic:claude-sonnet-4-5-20250929",
            "system_prompt": "be brief",
        },
    )

    events: list[EngineEvent] = []
    async for ev in engine.start_session(ctx):
        events.append(ev)

    kinds = [ev.kind for ev in events]
    assert kinds == ["usage_snapshot", "sdk_message"]
    usage_ev, sdk_ev = events
    assert usage_ev.cost_semantic == "delta"
    assert usage_ev.input_tokens == 12
    assert usage_ev.output_tokens == 3
    # Anthropic returns blocks; the assistant text is in the first text block.
    assert sdk_ev.payload.content[0].text == "Hello there."

    # Verify the call shape — system is top-level, not in messages.
    call_kwargs = fake_client.messages.create.await_args_list[0].kwargs
    assert call_kwargs["model"] == "claude-sonnet-4-5-20250929"
    assert call_kwargs["system"] == "be brief"
    assert call_kwargs["messages"] == [{"role": "user", "content": "say hi"}]


@pytest.mark.asyncio
async def test_anthropic_backend_tool_use_routes_through_permission(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    perm_log: list[PermissionRequest] = []

    async def _perm(req: PermissionRequest) -> dict[str, Any]:
        perm_log.append(req)
        return {"behavior": "deny", "message": "no read"}

    first = _anthropic_response(
        content_blocks=[
            _tool_use_block("tu_1", "Read", {"file_path": "secret.txt"})
        ],
        stop_reason="tool_use",
    )
    second = _anthropic_response(
        content_blocks=[_text_block("Understood, I cannot.")],
        stop_reason="end_turn",
    )

    fake_client = AsyncMock()
    fake_client.messages.create = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(
        "computeragent.engines.git_agent._build_anthropic_client",
        lambda: fake_client,
    )

    engine = GitAgentEngine()
    ctx = _ctx(
        user_messages=[{"role": "user", "content": "read secret.txt"}],
        options={"model": "claude-sonnet-4-5", "system_prompt": "be careful"},
        on_perm=_perm,
    )

    events: list[EngineEvent] = []
    async for ev in engine.start_session(ctx):
        events.append(ev)

    # PermissionRequest must have been routed.
    assert len(perm_log) == 1
    assert perm_log[0].tool_name == "Read"
    # A `tool` payload sdk_message landed for the deny.
    tool_msgs = [
        e for e in events
        if e.kind == "sdk_message"
        and isinstance(e.payload, dict)
        and e.payload.get("role") == "tool"
    ]
    assert len(tool_msgs) == 1
    assert "denied" in tool_msgs[0].payload["content"]
    # Final assistant message landed too.
    assistant_msgs = [
        e for e in events
        if e.kind == "sdk_message"
        and not (isinstance(e.payload, dict) and e.payload.get("role") == "tool")
    ]
    assert len(assistant_msgs) == 2  # first response + second response


@pytest.mark.asyncio
async def test_anthropic_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    engine = GitAgentEngine()
    ctx = _ctx(
        user_messages=[{"role": "user", "content": "hi"}],
        options={"model": "claude-sonnet-4-5"},
    )
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        async for _ in engine.start_session(ctx):
            break


@pytest.mark.asyncio
async def test_anthropic_multi_turn_carries_context(monkeypatch):
    """Second turn's `system` arg must include the prior assistant reply
    so the agent has memory across .chat() calls."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    fake_client = AsyncMock()
    fake_client.messages.create = AsyncMock(
        side_effect=[
            _anthropic_response(content_blocks=[_text_block("first reply")]),
            _anthropic_response(content_blocks=[_text_block("second reply")]),
        ]
    )
    monkeypatch.setattr(
        "computeragent.engines.git_agent._build_anthropic_client",
        lambda: fake_client,
    )

    engine = GitAgentEngine()
    ctx = _ctx(
        user_messages=[
            {"role": "user", "content": "q1"},
            {"role": "user", "content": "q2"},
        ],
        options={"model": "claude-sonnet-4-5", "system_prompt": "be brief"},
    )

    async for _ in engine.start_session(ctx):
        pass

    assert fake_client.messages.create.await_count == 2
    second_kwargs = fake_client.messages.create.await_args_list[1].kwargs
    # Prior turn's assistant text must be in system suffix.
    assert "first reply" in second_kwargs["system"]
    # The messages list should only contain THIS turn's user message
    # (prior turns went into system, not messages).
    assert second_kwargs["messages"] == [{"role": "user", "content": "q2"}]
