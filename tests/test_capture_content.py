"""Coverage for ``capture_content`` in the semconv-1.40 STRUCTURED model.

Content is accumulated across a turn and flushed at the turn boundary as:
  * ``gen_ai.input.messages``  = [{role, parts:[...]}]   (span attr / log body)
  * ``gen_ai.output.messages`` = [{role, parts:[...], finish_reason}]
  * ``gen_ai.system_instructions`` = [part, ...]
In ``events`` mode these land as ONE ``gen_ai.client.inference.operation.details``
log record; in ``attributes`` mode as JSON-string span attributes on the chat
span. Tool args/results stay bare ``gen_ai.tool.call.arguments`` / ``.result``
span attributes in every mode.
"""

from __future__ import annotations

import json

import pytest

opentelemetry = pytest.importorskip("opentelemetry")
from opentelemetry.sdk._logs import LoggerProvider  # noqa: E402
from opentelemetry.sdk._logs.export import (  # noqa: E402
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
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


def _build_inmemory() -> tuple[OtelSink, InMemorySpanExporter, InMemoryLogRecordExporter]:
    span_exp = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exp))

    log_exp = InMemoryLogRecordExporter()
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exp))

    sink = OtelSink(tracer_provider=tracer_provider, logger_provider=logger_provider)
    return sink, span_exp, log_exp


def _ev(kind, payload=None, sid="sess_c", agent_name="QA"):
    return TelemetryEvent(kind=kind, session_id=sid, agent_name=agent_name, payload=payload or {})


async def _run_one_turn_with_tool(sink: OtelSink) -> None:
    """Drive a synthetic single turn: session_started (identity + system
    prompt) → turn_started (user prompt) → assistant_message → tool_use →
    tool_result → session_ended (flushes content onto the chat span)."""
    await sink.emit(
        _ev(
            "session_started",
            {"model": "claude-sonnet-4-5", "system_prompt": "You are AcmeAssist."},
        )
    )
    await sink.emit(_ev("turn_started", {"prompt": "What's in CLAUDE.md?", "model": "claude-sonnet-4-5"}))
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


def _details_logs(log_exp: InMemoryLogRecordExporter):
    return [
        ld
        for ld in log_exp.get_finished_logs()
        if dict(ld.log_record.attributes or {}).get("event.name")
        == "gen_ai.client.inference.operation.details"
    ]


# ── disabled (default) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_content_off_by_default():
    sink, exp, log_exp = _build_inmemory()
    await _run_one_turn_with_tool(sink)

    spans = exp.get_finished_spans()
    for s in spans:
        attrs = dict(s.attributes or {})
        assert "gen_ai.input.messages" not in attrs
        assert "gen_ai.output.messages" not in attrs
        assert "gen_ai.system_instructions" not in attrs
        assert "gen_ai.tool.call.arguments" not in attrs
        assert "gen_ai.tool.call.result" not in attrs
    assert _details_logs(log_exp) == []


# ── attributes mode ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_content_attributes_mode(monkeypatch):
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT", "1")
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT_MODE", "attributes")
    sink, exp, log_exp = _build_inmemory()
    await _run_one_turn_with_tool(sink)

    spans = exp.get_finished_spans()
    chat = _span(spans, "invoke_agent")
    tool = _span(spans, "execute_tool")
    chat_attrs = dict(chat.attributes or {})

    # Structured messages on the chat span (JSON strings).
    assert json.loads(chat_attrs["gen_ai.input.messages"]) == [
        {"role": "user", "parts": [{"type": "text", "content": "What's in CLAUDE.md?"}]}
    ]
    assert json.loads(chat_attrs["gen_ai.system_instructions"]) == [
        {"type": "text", "content": "You are AcmeAssist."}
    ]
    out = json.loads(chat_attrs["gen_ai.output.messages"])
    assert out == [
        {
            "role": "assistant",
            "parts": [{"type": "text", "content": "Let me read it."}],
            "finish_reason": "stop",
        }
    ]
    # No flat 1.27 keys anymore.
    assert "gen_ai.prompt.0.content" not in chat_attrs
    assert "gen_ai.completion.0.content" not in chat_attrs

    # Tool content stays BARE on the execute_tool span.
    tool_attrs = dict(tool.attributes or {})
    assert json.loads(tool_attrs["gen_ai.tool.call.arguments"]) == {"file_path": "CLAUDE.md"}
    assert tool_attrs["gen_ai.tool.call.result"] == "You are AcmeAssist."

    # attributes mode emits no inference-details log record.
    assert _details_logs(log_exp) == []


# ── events mode ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_content_events_mode(monkeypatch):
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT", "1")
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT_MODE", "events")
    sink, exp, log_exp = _build_inmemory()
    await _run_one_turn_with_tool(sink)

    logs = _details_logs(log_exp)
    assert len(logs) == 1
    lr = logs[0].log_record
    body = dict(lr.body or {})
    assert body["gen_ai.input.messages"] == [
        {"role": "user", "parts": [{"type": "text", "content": "What's in CLAUDE.md?"}]}
    ]
    assert body["gen_ai.system_instructions"] == [
        {"type": "text", "content": "You are AcmeAssist."}
    ]
    assert body["gen_ai.output.messages"][0]["finish_reason"] == "stop"
    attrs = dict(lr.attributes or {})
    assert attrs["gen_ai.conversation.id"] == "sess_c"
    assert attrs["gen_ai.provider.name"] == "anthropic"
    assert attrs["gen_ai.request.model"] == "claude-sonnet-4-5"

    # No structured content *attributes* on spans in events mode.
    for s in exp.get_finished_spans():
        sa = dict(s.attributes or {})
        assert "gen_ai.input.messages" not in sa
        assert "gen_ai.output.messages" not in sa

    # Tool content still bare span attributes (span-only in every mode).
    tool = _span(exp.get_finished_spans(), "execute_tool")
    ta = dict(tool.attributes or {})
    assert json.loads(ta["gen_ai.tool.call.arguments"]) == {"file_path": "CLAUDE.md"}
    assert ta["gen_ai.tool.call.result"] == "You are AcmeAssist."


# ── both mode ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_content_both_mode(monkeypatch):
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT", "1")
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT_MODE", "both")
    sink, exp, log_exp = _build_inmemory()
    await _run_one_turn_with_tool(sink)

    chat = _span(exp.get_finished_spans(), "invoke_agent")
    chat_attrs = dict(chat.attributes or {})
    # Attribute path populated.
    assert json.loads(chat_attrs["gen_ai.input.messages"])[0]["parts"][0]["content"] == (
        "What's in CLAUDE.md?"
    )
    # Event path populated too.
    logs = _details_logs(log_exp)
    assert len(logs) == 1
    assert dict(logs[0].log_record.body or {})["gen_ai.input.messages"][0]["parts"][0][
        "content"
    ] == "What's in CLAUDE.md?"


# ── truncation ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_content_truncates_oversized_payload(monkeypatch):
    """Content longer than ``max_attribute_length`` is truncated with the TS
    marker ``..."[truncated:N]"`` carrying the untruncated length."""
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT", "1")
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT_MODE", "attributes")
    monkeypatch.setenv("COMPUTERAGENT_MAX_ATTRIBUTE_LENGTH", "100")

    big_prompt = "x" * 500
    sink, exp, _ = _build_inmemory()
    await sink.emit(_ev("session_started", {"model": "m", "system_prompt": "sys"}))
    await sink.emit(_ev("turn_started", {"prompt": big_prompt, "model": "m"}))
    await sink.emit(_ev("assistant_message", {"model": "m", "text": "ok"}))
    await sink.emit(_ev("session_ended", {"is_error": False, "duration_ms": 1.0}))

    chat = _span(exp.get_finished_spans(), "invoke_agent")
    content = (chat.attributes or {}).get("gen_ai.input.messages")
    assert content is not None
    # head is (max-32) chars + the ..."[truncated:N]" marker → bounded by ~max.
    assert len(content) <= 100
    assert '..."[truncated:' in content
    assert content.startswith("[")  # JSON array head preserved


# ── streaming-mode sentinel ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_prompt_sentinel_not_captured(monkeypatch):
    """A ``"<streaming>"`` turn prompt must NOT be captured as user input."""
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT", "1")
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT_MODE", "attributes")
    sink, exp, _ = _build_inmemory()
    await sink.emit(_ev("session_started", {"model": "m", "system_prompt": "sys"}))
    await sink.emit(_ev("turn_started", {"prompt": "<streaming>", "model": "m"}))
    await sink.emit(_ev("assistant_message", {"model": "m", "text": "ok"}))
    await sink.emit(_ev("session_ended", {"is_error": False, "duration_ms": 1.0}))

    chat = _span(exp.get_finished_spans(), "invoke_agent")
    attrs = dict(chat.attributes or {})
    # System prompt was real → captured.
    assert json.loads(attrs["gen_ai.system_instructions"]) == [{"type": "text", "content": "sys"}]
    # User prompt was the sentinel → no input messages.
    assert "gen_ai.input.messages" not in attrs


# ── garbage env values → safe default ────────────────────────────────────


@pytest.mark.asyncio
async def test_garbage_mode_falls_back_to_events(monkeypatch):
    """Typo in COMPUTERAGENT_CAPTURE_CONTENT_MODE falls back to events."""
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT", "1")
    monkeypatch.setenv("COMPUTERAGENT_CAPTURE_CONTENT_MODE", "atributs")  # typo
    sink, exp, log_exp = _build_inmemory()
    await _run_one_turn_with_tool(sink)

    # events is the fallback default → one inference-details log record.
    assert len(_details_logs(log_exp)) == 1
