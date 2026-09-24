"""ClaudeAgentEngine — policy enforcement that survives prompt-skipping
permission modes via a PreToolUse hook (parity with the TS engine)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from computeragent.engines.claude_agent import (
    _build_options,
    _policy_pretooluse_matcher,
)


def _ctx(options: dict[str, Any], policy_decider: Any = None) -> Any:
    return SimpleNamespace(
        manifest=SimpleNamespace(name="t-agent", options=options),
        workspace_path="/tmp/ws",
        envs={},
        session_id="ca_test",
        session_store=None,
        resume_session_id=None,
        policy_decider=policy_decider,
    )


async def _allow_decider(*_args: Any) -> dict[str, Any]:
    return {"behavior": "allow"}


def test_hook_registered_under_bypass_permissions():
    options = _build_options(_ctx({"permission_mode": "bypassPermissions"}, _allow_decider))
    hooks = options.hooks or {}
    assert len(hooks.get("PreToolUse") or []) == 1
    assert options.can_use_tool is not None


def test_no_hook_in_default_mode():
    # Default mode routes gating through can_use_tool — no hook, no double eval.
    options = _build_options(_ctx({}, _allow_decider))
    assert not (options.hooks or {})
    assert options.can_use_tool is not None


def test_no_hook_without_policy_decider():
    options = _build_options(_ctx({"permission_mode": "bypassPermissions"}))
    assert not (options.hooks or {})
    assert options.can_use_tool is None


def test_hook_merges_with_existing_hooks():
    from claude_agent_sdk.types import HookMatcher

    async def noop(*_a: Any) -> dict[str, Any]:
        return {}

    existing = {"PreToolUse": [HookMatcher(hooks=[noop])]}
    options = _build_options(
        _ctx({"permission_mode": "bypassPermissions", "hooks": existing}, _allow_decider)
    )
    assert len(options.hooks["PreToolUse"]) == 2  # GAP/caller hook + policy hook


@pytest.mark.asyncio
async def test_hook_maps_deny_to_permission_decision():
    async def decider(tool_name: str, tool_input: dict[str, Any], _ctx: Any) -> dict[str, Any]:
        assert tool_name == "Bash"
        assert tool_input == {"command": "rm -rf /"}
        return {"behavior": "deny", "message": "too dangerous"}

    matcher = _policy_pretooluse_matcher(decider)
    out = await matcher.hooks[0](
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, "toolu_1", None
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "too dangerous"


@pytest.mark.asyncio
async def test_hook_allow_returns_no_opinion():
    matcher = _policy_pretooluse_matcher(_allow_decider)
    assert await matcher.hooks[0]({"tool_name": "Read", "tool_input": {}}, None, None) == {}


@pytest.mark.asyncio
async def test_hook_handles_dataclass_style_results():
    class Deny:
        behavior = "deny"
        message = "nope"

    async def decider(*_a: Any) -> Any:
        return Deny()

    matcher = _policy_pretooluse_matcher(decider)
    out = await matcher.hooks[0]({"tool_name": "Write", "tool_input": {}}, None, None)
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "nope"


@pytest.mark.asyncio
async def test_hook_fails_closed_on_decider_exception():
    async def decider(*_a: Any) -> Any:
        raise RuntimeError("boom")

    matcher = _policy_pretooluse_matcher(decider)
    out = await matcher.hooks[0]({"tool_name": "Bash", "tool_input": {}}, None, None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "policy evaluation failed" in out["hookSpecificOutput"]["permissionDecisionReason"]
