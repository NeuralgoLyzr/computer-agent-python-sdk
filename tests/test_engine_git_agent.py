"""Coverage for ``GitAgentEngine`` — the Python port of the gitclaw loop.

The engine talks to an OpenAI-compatible endpoint, so we mock the
``openai.AsyncOpenAI`` client surface. Tests cover:

* env-var fail-fast paths
* single-turn happy path with usage emission
* tool-call branch routes through ``ctx.on_permission_request``
* multi-turn context restoration via the system-prompt suffix
* cancel_event short-circuits the loop
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from computeragent.engines.git_agent import GitAgentEngine
from computeragent.protocol_types import (
    AgentManifest,
    EngineContext,
    EngineEvent,
    PermissionRequest,
)


def _stub_choice(*, content: str | None, tool_calls: list[Any] | None = None) -> Any:
    """Minimal stand-in for an OpenAI ``ChatCompletionChoice`` message."""

    class _Msg:
        def __init__(self) -> None:
            self.content = content
            self.tool_calls = tool_calls

    class _Choice:
        def __init__(self) -> None:
            self.message = _Msg()

    return _Choice()


def _stub_response(*, choices: list[Any], usage: dict[str, int]) -> Any:
    class _Usage:
        def __init__(self) -> None:
            self.prompt_tokens = usage.get("prompt_tokens")
            self.completion_tokens = usage.get("completion_tokens")

    class _Resp:
        def __init__(self) -> None:
            self.choices = choices
            self.usage = _Usage()

    return _Resp()


def _stub_tool_call(*, call_id: str, name: str, arguments: str) -> Any:
    class _Func:
        def __init__(self) -> None:
            self.name = name
            self.arguments = arguments

    class _TC:
        def __init__(self) -> None:
            self.id = call_id
            self.function = _Func()
            self.type = "function"

    return _TC()


async def _no_permission_needed(_req: PermissionRequest) -> dict[str, Any]:
    return {"behavior": "allow", "updated_input": {}}


def _ctx(
    *,
    user_messages: list[dict[str, Any]],
    options: dict[str, Any],
    on_perm=_no_permission_needed,
) -> EngineContext:
    """Build a minimal EngineContext for the tests."""

    async def _queue():
        for msg in user_messages:
            yield msg

    from pathlib import Path

    return EngineContext(
        session_id="sess_test",
        manifest=AgentManifest(name="git-agent-test", options=options),
        workspace_path=Path("/tmp"),
        envs={},
        user_message_queue=_queue(),
        on_permission_request=on_perm,
        cancel_event=asyncio.Event(),
    )


@pytest.mark.asyncio
async def test_missing_base_url_raises(monkeypatch):
    monkeypatch.delenv("GITCLAW_MODEL_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "x")

    engine = GitAgentEngine()
    ctx = _ctx(
        user_messages=[{"role": "user", "content": "hi"}],
        options={"model": "gpt-4o-mini"},
    )
    with pytest.raises(RuntimeError, match="GITCLAW_MODEL_BASE_URL"):
        async for _ in engine.start_session(ctx):
            break


@pytest.mark.asyncio
async def test_missing_api_key_raises(monkeypatch):
    monkeypatch.setenv("GITCLAW_MODEL_BASE_URL", "https://api.example.com/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    engine = GitAgentEngine()
    ctx = _ctx(
        user_messages=[{"role": "user", "content": "hi"}],
        options={"model": "gpt-4o-mini"},
    )
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        async for _ in engine.start_session(ctx):
            break


@pytest.mark.asyncio
async def test_missing_model_raises(monkeypatch):
    monkeypatch.setenv("GITCLAW_MODEL_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    engine = GitAgentEngine()
    ctx = _ctx(
        user_messages=[{"role": "user", "content": "hi"}],
        options={},  # no 'model'
    )
    with pytest.raises(ValueError, match="options\\['model'\\]"):
        async for _ in engine.start_session(ctx):
            break


@pytest.mark.asyncio
async def test_single_turn_happy_path(monkeypatch):
    monkeypatch.setenv("GITCLAW_MODEL_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "x")

    # Patch _build_openai_client so the real openai client isn't constructed.
    fake_client = AsyncMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_stub_response(
            choices=[_stub_choice(content="Hello there.")],
            usage={"prompt_tokens": 12, "completion_tokens": 3},
        )
    )
    monkeypatch.setattr(
        "computeragent.engines.git_agent._build_openai_client",
        lambda: fake_client,
    )

    engine = GitAgentEngine()
    ctx = _ctx(
        user_messages=[{"role": "user", "content": "hello"}],
        options={"model": "gpt-4o-mini", "system_prompt": "be brief"},
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
    assert sdk_ev.payload.content == "Hello there."


@pytest.mark.asyncio
async def test_tool_call_routes_through_permission_hook(monkeypatch):
    monkeypatch.setenv("GITCLAW_MODEL_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "x")

    call_log: list[PermissionRequest] = []

    async def _capture_perm(req: PermissionRequest) -> dict[str, Any]:
        call_log.append(req)
        return {"behavior": "deny", "message": "no thanks"}

    # First completion → emits tool_call; second completion → final text.
    first = _stub_response(
        choices=[
            _stub_choice(
                content=None,
                tool_calls=[_stub_tool_call(call_id="tc1", name="Read", arguments='{"file": "x"}')],
            )
        ],
        usage={"prompt_tokens": 5, "completion_tokens": 5},
    )
    second = _stub_response(
        choices=[_stub_choice(content="Sorry, I cannot.")],
        usage={"prompt_tokens": 8, "completion_tokens": 4},
    )

    fake_client = AsyncMock()
    fake_client.chat.completions.create = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(
        "computeragent.engines.git_agent._build_openai_client",
        lambda: fake_client,
    )

    engine = GitAgentEngine()
    ctx = _ctx(
        user_messages=[{"role": "user", "content": "look up X"}],
        options={"model": "gpt-4o-mini"},
        on_perm=_capture_perm,
    )

    events: list[EngineEvent] = []
    async for ev in engine.start_session(ctx):
        events.append(ev)

    assert len(call_log) == 1
    assert call_log[0].tool_name == "Read"
    # Tool-result message inserted between the two assistant messages.
    sdk_payloads = [ev.payload for ev in events if ev.kind == "sdk_message"]
    tool_results = [p for p in sdk_payloads if isinstance(p, dict) and p.get("role") == "tool"]
    assert tool_results, "tool result should be emitted as sdk_message"
    assert "denied" in tool_results[0]["content"]


@pytest.mark.asyncio
async def test_multi_turn_carries_context(monkeypatch):
    """After turn 1, the system prompt should mention prior turns."""
    monkeypatch.setenv("GITCLAW_MODEL_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "x")

    fake_client = AsyncMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=[
            _stub_response(
                choices=[_stub_choice(content="reply-1")],
                usage={"prompt_tokens": 1, "completion_tokens": 1},
            ),
            _stub_response(
                choices=[_stub_choice(content="reply-2")],
                usage={"prompt_tokens": 1, "completion_tokens": 1},
            ),
        ]
    )
    monkeypatch.setattr(
        "computeragent.engines.git_agent._build_openai_client",
        lambda: fake_client,
    )

    engine = GitAgentEngine()
    ctx = _ctx(
        user_messages=[
            {"role": "user", "content": "q1"},
            {"role": "user", "content": "q2"},
        ],
        options={"model": "gpt-4o-mini", "system_prompt": "be brief"},
    )

    async for _ in engine.start_session(ctx):
        pass

    # Two completions fired, one per user message.
    assert fake_client.chat.completions.create.await_count == 2
    second_call_messages = fake_client.chat.completions.create.await_args_list[1].kwargs["messages"]
    # Second-turn system prompt must include the prior assistant output.
    sys_msg = next(m for m in second_call_messages if m["role"] == "system")
    assert "reply-1" in sys_msg["content"]


@pytest.mark.asyncio
async def test_cancel_event_short_circuits_loop(monkeypatch):
    monkeypatch.setenv("GITCLAW_MODEL_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "x")

    fake_client = AsyncMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=_stub_response(
            choices=[_stub_choice(content="ok")],
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )
    )
    monkeypatch.setattr(
        "computeragent.engines.git_agent._build_openai_client",
        lambda: fake_client,
    )

    engine = GitAgentEngine()
    ctx = _ctx(
        user_messages=[{"role": "user", "content": "hi"}],
        options={"model": "gpt-4o-mini"},
    )
    ctx.cancel_event.set()  # Pre-cancel — loop should exit immediately.

    events = [ev async for ev in engine.start_session(ctx)]
    assert events == []
    assert fake_client.chat.completions.create.await_count == 0
