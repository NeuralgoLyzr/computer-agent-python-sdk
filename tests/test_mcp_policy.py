"""Governance of MCP tool calls — the property that justifies `engine` mode.

`mcp_mode: engine` exists so the harness owns the tool loop: every call produces
an `execute_tool` span and passes the policy decider. `studio` mode is refused
precisely because it removes both. So these are not incidental tests — they are
the ones that make the refusal in `agentos_agent` honest.

The `bypassPermissions` assertions carry the weight. That is the mode this run
mode dispatches, and it is the mode where `allowed_tools` grants and forbids
nothing — so the PreToolUse hook is the only gate that actually fires.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from computeragent.engines.claude_agent import _build_options, _policy_pretooluse_matcher

MCP_TOOL = "mcp__Weather__get_weather"


def ctx(options: dict[str, Any], decider: Any = None) -> Any:
    return SimpleNamespace(
        manifest=SimpleNamespace(name="t-agent", options=options),
        workspace_path="/tmp/ws",
        envs={},
        session_id="ca_test",
        session_store=None,
        resume_session_id=None,
        policy_decider=decider,
    )


def deny_mcp():
    """Denies anything namespaced `mcp__`, allows everything else."""

    async def decider(tool_name: str, _input: Any, _context: Any) -> dict[str, Any]:
        if tool_name.startswith("mcp__"):
            return {"behavior": "deny", "message": f"{tool_name} is not permitted"}
        return {"behavior": "allow"}

    return decider


# ── the gate that fires under the mode this SDK dispatches ───────────────────


async def test_an_mcp_call_is_denied_under_bypass_permissions():
    """THE assertion. `can_use_tool` is skipped entirely under a prompt-skipping
    mode, so without the hook a console-bound policy would be unenforced on
    exactly the runs this SDK produces."""
    options = _build_options(ctx({"permission_mode": "bypassPermissions"}, deny_mcp()))

    (matcher,) = options.hooks["PreToolUse"]
    (hook,) = matcher.hooks
    out = await hook({"tool_name": MCP_TOOL, "tool_input": {"city": "Pune"}}, "tu_1", None)

    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert MCP_TOOL in decision["permissionDecisionReason"]


async def test_a_permitted_mcp_call_passes_the_hook():
    options = _build_options(ctx({"permission_mode": "bypassPermissions"}, deny_mcp()))
    (matcher,) = options.hooks["PreToolUse"]
    (hook,) = matcher.hooks

    assert await hook({"tool_name": "Read", "tool_input": {}}, "tu_2", None) == {}


async def test_an_mcp_call_is_denied_under_default_mode_too():
    """Here the gate is `can_use_tool`, which the CLI consults when its own
    permission rules evaluate to "ask"."""
    options = _build_options(ctx({}, deny_mcp()))

    out = await options.can_use_tool(MCP_TOOL, {"city": "Pune"}, SimpleNamespace())

    assert out["behavior"] == "deny"


@pytest.mark.parametrize("mode", ["bypassPermissions", "dontAsk", "auto", "acceptEdits"])
async def test_every_prompt_skipping_mode_still_gets_a_gate(mode):
    """All four skip `can_use_tool`. A mode added to that set without a hook
    would silently un-govern MCP calls."""
    options = _build_options(ctx({"permission_mode": mode}, deny_mcp()))

    (matcher,) = options.hooks["PreToolUse"]
    (hook,) = matcher.hooks
    out = await hook({"tool_name": MCP_TOOL, "tool_input": {}}, "tu_3", None)

    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_the_hook_sees_the_mcp_tools_arguments():
    """A policy that inspects arguments — a destination host, a ticket project —
    needs them, and the qualified name alone is not enough."""
    seen: list[tuple[str, Any]] = []

    async def recorder(tool_name: str, tool_input: Any, _context: Any) -> dict[str, Any]:
        seen.append((tool_name, tool_input))
        return {"behavior": "allow"}

    matcher = _policy_pretooluse_matcher(recorder)
    (hook,) = matcher.hooks
    await hook({"tool_name": MCP_TOOL, "tool_input": {"city": "Pune"}}, "tu_4", None)

    assert seen == [(MCP_TOOL, {"city": "Pune"})]


# ── telemetry ────────────────────────────────────────────────────────────────


async def test_an_mcp_tool_use_mints_an_execute_tool_span():
    """Spans are minted from observed tool-use blocks — which is exactly what
    `studio` mode would leave nothing to mint from, and the concrete reason it
    is refused rather than supported."""
    pytest.importorskip("opentelemetry")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from computeragent.telemetry import TelemetryEvent
    from computeragent.telemetry.sinks import OtelSink

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    sink = OtelSink(tracer_provider=provider)

    def ev(kind, payload):
        return TelemetryEvent(kind=kind, session_id="s1", agent_name="t", payload=payload)

    await sink.emit(ev("session_started", {"engine": "claude-agent-sdk"}))
    await sink.emit(
        ev("tool_use", {"tool_name": MCP_TOOL, "tool_use_id": "tu_1", "tool_input": {"city": "P"}})
    )
    await sink.emit(ev("tool_result", {"tool_use_id": "tu_1", "is_error": False}))
    await sink.emit(ev("session_ended", {"is_error": False, "duration_ms": 1.0}))

    tool_spans = [s for s in exporter.get_finished_spans() if s.name.startswith("execute_tool")]

    assert len(tool_spans) == 1
    # The qualified name on both, so a trace can be filtered to MCP activity.
    assert MCP_TOOL in tool_spans[0].name
    assert tool_spans[0].attributes["gen_ai.tool.name"] == MCP_TOOL


def test_the_guardrail_glob_matches_generated_mcp_names():
    """`mcp__*` is the pattern operators write; it has to match what this SDK
    actually generates, not what the docs say it generates."""
    from computeragent.telemetry.middleware.guardrails import _matches_any

    assert _matches_any(MCP_TOOL, {"mcp__*"})
    assert _matches_any("mcp__Weather__get_weather", {"mcp__Weather__*"})
    assert not _matches_any("Read", {"mcp__*"})
    assert not _matches_any(MCP_TOOL, {"mcp__Docs__*"})
