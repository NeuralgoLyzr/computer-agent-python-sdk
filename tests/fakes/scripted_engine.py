"""A scripted :class:`EngineDriver` for orchestration tests.

Yields a fixed reply per turn without touching the Claude CLI, so the harness's
own drive loop — the roster fan-out, the closing synthesis turn, the wave events
— can be exercised end to end in-process.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

from computeragent.protocol_types import EngineCapabilities, EngineEvent

_CAPABILITIES = EngineCapabilities(
    streaming_input=True,
    partial_messages=True,
    permission_callback=True,
    sessions=True,
    budget=True,
)


def result_message(text: str, *, session_id: str = "eng_1", cost: float = 0.01) -> Any:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        total_cost_usd=cost,
        usage={"input_tokens": 3, "output_tokens": 5},
        result=text,
    )


def assistant_message(text: str, *, model: str = "test-model") -> Any:
    return AssistantMessage(content=[TextBlock(text=text)], model=model)


class ScriptedEngine:
    """Replies with ``replies[i]`` on turn ``i``, then repeats the last one."""

    name = "claude-agent-sdk"
    capabilities = _CAPABILITIES

    def __init__(self, replies: list[str], *, cost: float = 0.01) -> None:
        self.replies = list(replies)
        self.cost = cost
        #: Every prompt this engine was handed, in order. The closing synthesis
        #: turn is the interesting one, and asserting on it is the only way to
        #: tell a digest prompt from a direct-answer prompt.
        self.prompts: list[str] = []
        self.contexts: list[Any] = []

    async def start_session(self, ctx: Any):  # noqa: ANN201 - async generator
        self.contexts.append(ctx)
        async for entry in ctx.user_message_queue:
            self.prompts.append(str(entry.get("content", "")))
            break
        index = min(len(self.prompts) - 1, len(self.replies) - 1)
        text = self.replies[index] if self.replies else ""
        yield EngineEvent(kind="sdk_message", payload=assistant_message(text))
        yield EngineEvent(kind="sdk_message", payload=result_message(text, cost=self.cost))
        yield EngineEvent(
            kind="usage_snapshot",
            input_tokens=3,
            output_tokens=5,
            cost_usd=self.cost,
            cost_semantic="cumulative",
        )


class FailingEngine:
    """Raises on the first turn — the fail-soft path for a worker."""

    name = "claude-agent-sdk"
    capabilities = _CAPABILITIES

    def __init__(self, message: str = "engine exploded") -> None:
        self.message = message

    async def start_session(self, ctx: Any):  # noqa: ANN201 - async generator
        async for _ in ctx.user_message_queue:
            break
        raise RuntimeError(self.message)
        yield  # pragma: no cover - makes this an async generator


__all__ = [
    "FailingEngine",
    "ScriptedEngine",
    "assistant_message",
    "result_message",
]
