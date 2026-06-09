"""Unit tests for the coordinator's first-token detector that drives the
time-to-first-token signal. Duck-typed on the claude-agent-sdk ``StreamEvent``
shape (no hard import), so non-streaming engines never trip it."""

from __future__ import annotations

from computeragent.harness.coordinator import _is_first_token_stream_event


class StreamEvent:  # name matters: detector keys on type(payload).__name__
    def __init__(self, event):
        self.event = event


class AssistantMessage:  # a non-streaming complete message
    pass


def test_content_block_delta_is_first_token():
    assert _is_first_token_stream_event(StreamEvent({"type": "content_block_delta"})) is True


def test_content_block_start_is_first_token():
    assert _is_first_token_stream_event(StreamEvent({"type": "content_block_start"})) is True


def test_message_start_is_not_first_token():
    # message_start opens the stream before any content — would understate TTFT.
    assert _is_first_token_stream_event(StreamEvent({"type": "message_start"})) is False


def test_non_streamevent_payloads_never_match():
    # Complete messages, plain dicts, and other engines' payloads are ignored.
    assert _is_first_token_stream_event(AssistantMessage()) is False
    assert _is_first_token_stream_event({"type": "content_block_delta"}) is False
    assert _is_first_token_stream_event(None) is False


def test_streamevent_with_object_event_attribute():
    # Some SDK versions expose `.event` as an object with `.type`, not a dict.
    class _Inner:
        type = "content_block_delta"

    assert _is_first_token_stream_event(StreamEvent(_Inner())) is True
