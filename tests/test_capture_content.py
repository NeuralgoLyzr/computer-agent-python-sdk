"""Regression coverage for ``capture_content`` actually capturing content
in 0.1.3.

Prior versions accepted the config value but the OtelSink never read it —
no GenAI prompt/completion/tool content attributes ever landed on spans.
These tests lock the four shapes (attributes / events / both / disabled)
and the truncation behaviour that protects OTLP per-attribute limits.
"""

from __future__ import annotations

import pytest

opentelemetry = pytest.importorskip("opentelemetry")
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from computeragent.telemetry import TelemetryEvent  # noqa: E402
from computeragent.telemetry.config import _reset_for_tests  # noqa: E402
from computeragent.telemetry.sinks import OtelSink  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_config_state():
    """Start every test with empty _CONFIG so prior-test capture_content
    settings don't leak through."""
    _reset_for_tests()
    yield
    _reset_for_tests()


def _build_inmemory(**kwargs) -> tuple[OtelSink, InMemorySpanExporter]:
    span_exp = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exp))
    sink = OtelSink(tracer_provider=tracer_provider, **kwargs)
    return sink, span_exp


def _ev(kind, payload=None, sid="sess_c", agent_name="QA"):
    return TelemetryEvent(kind=kind, session_id=sid, agent_name=agent_name, payload=payload or {})


async def _run_one_turn_with_tool(sink: OtelSink) -> None:
    """Drive a synthetic session: session_started → assistant_message →
    tool_use → tool_result → session_ended. Mirrors a real agent loop
    that calls one tool and returns one assistant message."""
    await sink.emit(
        _ev(
            "session_started",
            {
                "model": "claude-sonnet-4-5",
                "system_prompt": "You are AcmeAssist.",
                "prompt": "What's in CLAUDE.md?",
            },
        )
    )
    await sink.emit(
        _ev("assistant_message", {"model": "claude-sonnet-4-5", "text": "Let me read it."})
    )
    await sink.emit(
        _ev(
            "tool_use",
            {
                "tool_use_id": "tu_1",
                "tool_name": "Read",
                "tool_input": {"file_path": "CLAUDE.md"},
            },
        )
    )
    await sink.emit(
        _ev(
            "tool_result",
            {"tool_use_id": "tu_1", "is_error": False, "content": "You are AcmeAssist."},
        )
    )
    await sink.emit(
        _ev(
            "session_ended",
            {"is_error": False, "duration_ms": 1234.0, "result": "It says 'You are AcmeAssist.'"},
        )
    )


def _span(spans, prefix):
    matching = [s for s in spans if s.name.startswith(prefix)]
    assert len(matching) == 1, f"expected exactly one {prefix} span; got {len(matching)}"
    return matching[0]


# ── disabled (default) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_content_off_by_default():
    """No env var, no kwarg → no prompt / completion / tool content
    attributes anywhere. Backwards-compat for 0.1.2 consumers."""
    sink, exp = _build_inmemory()
    await _run_one_turn_with_tool(sink)

    spans = exp.get_finished_spans()
    invoke = _span(spans, "invoke_agent")
    chat = _span(spans, "chat ")
    tool = _span(spans, "execute_tool")

    for s in (invoke, chat, tool):
        attrs = dict(s.attributes or {})
        assert not any(k.startswith("gen_ai.prompt.") for k in attrs)
        assert not any(k.startswith("gen_ai.completion.") for k in attrs)
        assert not any(k.startswith("gen_ai.tool.call.arguments") for k in attrs)
        assert not any(k.startswith("gen_ai.tool.call.result") for k in attrs)


# ── attributes mode ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_content_attributes_mode(monkeypatch):
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT", "1")
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT_MODE", "attributes")
    sink, exp = _build_inmemory()
    await _run_one_turn_with_tool(sink)

    spans = exp.get_finished_spans()
    invoke = _span(spans, "invoke_agent")
    chat = _span(spans, "chat ")
    tool = _span(spans, "execute_tool")

    invoke_attrs = dict(invoke.attributes or {})
    chat_attrs = dict(chat.attributes or {})
    tool_attrs = dict(tool.attributes or {})

    # invoke_agent: system prompt + initial user prompt + final answer
    assert invoke_attrs.get("gen_ai.prompt.0.role") == "system"
    assert invoke_attrs.get("gen_ai.prompt.0.content") == "You are AcmeAssist."
    assert invoke_attrs.get("gen_ai.prompt.1.role") == "user"
    assert invoke_attrs.get("gen_ai.prompt.1.content") == "What's in CLAUDE.md?"
    assert invoke_attrs.get("gen_ai.completion.final.role") == "assistant"
    assert invoke_attrs.get("gen_ai.completion.final.content") == "It says 'You are AcmeAssist.'"

    # chat: per-turn assistant text
    assert chat_attrs.get("gen_ai.completion.0.role") == "assistant"
    assert chat_attrs.get("gen_ai.completion.0.content") == "Let me read it."

    # execute_tool: JSON-encoded tool_input + tool result text
    import json

    args = tool_attrs.get("gen_ai.tool.call.arguments.content")
    assert args is not None
    assert json.loads(args) == {"file_path": "CLAUDE.md"}
    assert tool_attrs.get("gen_ai.tool.call.result.content") == "You are AcmeAssist."

    # In attributes mode, no span events fired for content.
    assert not any(
        e.name in {"gen_ai.user.message", "gen_ai.assistant.message", "gen_ai.system.message"}
        for s in spans
        for e in (s.events or [])
    )


# ── events mode ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_content_events_mode(monkeypatch):
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT", "1")
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT_MODE", "events")
    sink, exp = _build_inmemory()
    await _run_one_turn_with_tool(sink)

    spans = exp.get_finished_spans()
    invoke = _span(spans, "invoke_agent")
    chat = _span(spans, "chat ")
    tool = _span(spans, "execute_tool")

    invoke_events = {e.name: dict(e.attributes or {}) for e in (invoke.events or [])}
    chat_events = {e.name: dict(e.attributes or {}) for e in (chat.events or [])}
    tool_events = {e.name: dict(e.attributes or {}) for e in (tool.events or [])}

    assert invoke_events["gen_ai.system.message"]["content"] == "You are AcmeAssist."
    assert invoke_events["gen_ai.user.message"]["content"] == "What's in CLAUDE.md?"
    assert invoke_events["gen_ai.assistant.message"]["content"] == "It says 'You are AcmeAssist.'"
    assert chat_events["gen_ai.assistant.message"]["content"] == "Let me read it."
    assert "gen_ai.tool.call.start" in tool_events
    assert "gen_ai.tool.call.end" in tool_events

    # No content attributes in events mode.
    for s in (invoke, chat, tool):
        attrs = dict(s.attributes or {})
        assert not any(k.endswith(".content") and k.startswith("gen_ai.") for k in attrs)


# ── both mode ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_content_both_mode(monkeypatch):
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT", "1")
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT_MODE", "both")
    sink, exp = _build_inmemory()
    await _run_one_turn_with_tool(sink)

    spans = exp.get_finished_spans()
    invoke = _span(spans, "invoke_agent")

    attrs = dict(invoke.attributes or {})
    events = {e.name: dict(e.attributes or {}) for e in (invoke.events or [])}

    # Attribute path is populated.
    assert attrs.get("gen_ai.prompt.1.content") == "What's in CLAUDE.md?"
    # Event path is populated too.
    assert events["gen_ai.user.message"]["content"] == "What's in CLAUDE.md?"


# ── truncation ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_content_truncates_oversized_payload(monkeypatch):
    """Content longer than ``max_attribute_length`` is truncated with a
    visible ``...[truncated]`` marker — keeps OTLP packets under the
    per-attribute limit while still signalling the truncation downstream."""
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT", "1")
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT_MODE", "attributes")
    monkeypatch.setenv("COMPUTERAGENT_MAX_ATTRIBUTE_LENGTH", "100")

    big_prompt = "x" * 500
    sink, exp = _build_inmemory()
    await sink.emit(
        _ev(
            "session_started",
            {"model": "m", "system_prompt": "sys", "prompt": big_prompt},
        )
    )
    await sink.emit(_ev("session_ended", {"is_error": False, "duration_ms": 1.0}))

    invoke = _span(exp.get_finished_spans(), "invoke_agent")
    content = (invoke.attributes or {}).get("gen_ai.prompt.1.content")
    assert content is not None
    assert len(content) == 100
    assert content.endswith("...[truncated]")
    assert content.startswith("x" * 50)  # leading content preserved


# ── streaming-mode sentinel ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_prompt_sentinel_not_captured(monkeypatch):
    """When the caller streams the prompt (AsyncIterable), the proxy writes
    ``"<streaming>"`` instead of the body. We must NOT capture that
    sentinel as if it were the user's prompt — that'd be misleading."""
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT", "1")
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT_MODE", "attributes")
    sink, exp = _build_inmemory()
    await sink.emit(
        _ev(
            "session_started",
            {"model": "m", "system_prompt": "sys", "prompt": "<streaming>"},
        )
    )
    await sink.emit(_ev("session_ended", {"is_error": False, "duration_ms": 1.0}))

    invoke = _span(exp.get_finished_spans(), "invoke_agent")
    attrs = dict(invoke.attributes or {})
    # System prompt was a real value, so it landed.
    assert attrs.get("gen_ai.prompt.0.content") == "sys"
    # User prompt was the sentinel, so we skip it.
    assert "gen_ai.prompt.1.content" not in attrs
    assert "gen_ai.prompt.1.role" not in attrs


# ── garbage env values → safe default ────────────────────────────────────


@pytest.mark.asyncio
async def test_garbage_mode_falls_back_to_events(monkeypatch):
    """Typo in COMPUTERAGENT_CAPTURE_CONTENT_MODE shouldn't blow up — fall
    back to ``events`` (the default) silently."""
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT", "1")
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT_MODE", "atributs")  # typo
    sink, exp = _build_inmemory()
    await _run_one_turn_with_tool(sink)

    invoke = _span(exp.get_finished_spans(), "invoke_agent")
    events = {e.name for e in (invoke.events or [])}
    # `events` is the fallback default — span events should be present.
    assert "gen_ai.user.message" in events
