"""ClaudeAgentEngine policy wiring — the can_use_tool wrapper stamps the
harness session id onto the permission context so policy_decision telemetry
correlates with the session's live OTel spans (not session "unknown")."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from computeragent.engines.claude_agent import _bind_session_id


@dataclass
class _Ctx:
    """Stand-in for claude_agent_sdk's ToolPermissionContext (plain dataclass,
    accepts new attributes via setattr)."""

    tool_use_id: str = "toolu_1"


@pytest.mark.asyncio
async def test_bind_session_id_stamps_context_and_delegates():
    seen: dict[str, Any] = {}

    async def decider(tool_name: str, tool_input: dict[str, Any], context: Any) -> str:
        seen["tool_name"] = tool_name
        seen["tool_input"] = tool_input
        seen["session_id"] = getattr(context, "session_id", None)
        seen["tool_use_id"] = getattr(context, "tool_use_id", None)
        return "decision"

    wrapped = _bind_session_id(decider, "ca_sess_42")
    out = await wrapped("Bash", {"command": "ls"}, _Ctx())

    assert out == "decision"
    assert seen["tool_name"] == "Bash"
    assert seen["tool_input"] == {"command": "ls"}
    assert seen["session_id"] == "ca_sess_42"
    assert seen["tool_use_id"] == "toolu_1"


@pytest.mark.asyncio
async def test_bind_session_id_survives_unstampable_context():
    """A context that rejects attribute writes must not break the decider."""

    class _Frozen:
        __slots__ = ("tool_use_id",)

        def __init__(self) -> None:
            self.tool_use_id = "toolu_2"

    async def decider(tool_name: str, tool_input: dict[str, Any], context: Any) -> str:
        return "still-works"

    wrapped = _bind_session_id(decider, "ca_sess_43")
    assert await wrapped("Read", {}, _Frozen()) == "still-works"
