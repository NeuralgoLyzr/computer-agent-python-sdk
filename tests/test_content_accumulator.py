"""Unit tests for the structured content accumulator + helpers.

Pure — no OpenTelemetry import, so these run without the ``[otel]`` extra.
"""

from __future__ import annotations

from computeragent.telemetry.sinks.content import (
    ContentAccumulator,
    block_to_part,
    blocks_to_parts,
    map_finish_reason,
    serialize_bounded,
)


def test_record_user_text_and_snapshot():
    acc = ContentAccumulator()
    acc.record_user_text("s", "hello")
    assert acc.input_for("s") == [{"role": "user", "parts": [{"type": "text", "content": "hello"}]}]
    # empty text is a no-op
    acc.record_user_text("s", "")
    assert len(acc.input_for("s")) == 1


def test_record_user_blocks_filters_tool_result():
    acc = ContentAccumulator()
    acc.record_user_blocks(
        "s",
        [
            {"type": "text", "text": "look at this"},
            {"type": "tool_result", "tool_use_id": "t1", "content": "ignored as user input"},
        ],
    )
    msgs = acc.input_for("s")
    assert msgs == [{"role": "user", "parts": [{"type": "text", "content": "look at this"}]}]
    # a blocks list of ONLY tool_result records nothing (model plumbing).
    acc.record_user_blocks("s", [{"type": "tool_result", "tool_use_id": "t2", "content": "x"}])
    assert len(acc.input_for("s")) == 1


def test_record_assistant_text_finish_reasons():
    acc = ContentAccumulator()
    acc.record_assistant_text("s", "done", stop_reason="end_turn")
    acc.record_assistant_text("s", "calling", has_tool_use=True)  # inferred tool_call
    acc.record_assistant_text("s", "long", stop_reason="max_tokens")
    out = acc.output_for("s")
    assert [m["finish_reason"] for m in out] == ["stop", "tool_call", "length"]
    assert out[0]["parts"] == [{"type": "text", "content": "done"}]


def test_record_assistant_blocks_renames_and_infers():
    acc = ContentAccumulator()
    acc.record_assistant_blocks(
        "s",
        [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "let me check"},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "/x"}},
        ],
    )
    [msg] = acc.output_for("s")
    assert msg["parts"] == [
        {"type": "reasoning", "content": "hmm"},
        {"type": "text", "content": "let me check"},
        {"type": "tool_call", "name": "Read", "id": "t1", "arguments": {"path": "/x"}},
    ]
    # a tool_call part present + no explicit stop_reason → finish_reason tool_call
    assert msg["finish_reason"] == "tool_call"


def test_block_to_part_tool_result_rename():
    part = block_to_part({"type": "tool_result", "tool_use_id": "t9", "content": "result body"})
    assert part == {"type": "tool_call_response", "response": "result body", "id": "t9"}
    assert block_to_part({"type": "unknown"}) is None
    assert blocks_to_parts([{"type": "text", "text": "a"}, {"type": "weird"}]) == [
        {"type": "text", "content": "a"}
    ]


def test_system_instructions_no_role_wrapper():
    acc = ContentAccumulator()
    acc.record_system_instructions("s", "be helpful")
    assert acc.system_for("s") == [{"type": "text", "content": "be helpful"}]


def test_reset_clears_messages_not_tool_maps():
    acc = ContentAccumulator()
    acc.record_user_text("s", "hi")
    acc.record_assistant_text("s", "yo", stop_reason="end_turn")
    acc.record_tool_args("t1", {"a": 1})
    acc.reset("s")
    assert acc.input_for("s") == []
    assert acc.output_for("s") == []
    # tool args survive a turn reset until consumed.
    assert acc.take_tool_args("t1") == {"a": 1}
    assert acc.take_tool_args("t1") is None  # consumed


def test_take_tool_result_consumes():
    acc = ContentAccumulator()
    acc.record_tool_result("t1", "out")
    assert acc.take_tool_result("t1") == "out"
    assert acc.take_tool_result("t1") is None


def test_map_finish_reason_table():
    assert map_finish_reason("end_turn", False) == "stop"
    assert map_finish_reason("stop_sequence", False) == "stop"
    assert map_finish_reason("pause_turn", False) == "stop"
    assert map_finish_reason("max_tokens", False) == "length"
    assert map_finish_reason("tool_use", False) == "tool_call"
    assert map_finish_reason("refusal", False) == "content_filter"
    assert map_finish_reason(None, True) == "tool_call"
    assert map_finish_reason(None, False) == "stop"
    assert map_finish_reason("something_new", False) == "stop"


def test_serialize_bounded_marker():
    small = serialize_bounded({"k": "v"}, 1000)
    assert small == '{"k": "v"}'
    big = serialize_bounded({"k": "x" * 500}, 100)
    # head is (max-32) chars + the ..."[truncated:N]" marker → bounded, not exact.
    assert len(big) < 100
    assert '..."[truncated:' in big
