"""Content coercion — and the two places it deliberately differs from the TS.

`mcp.ts:362-428` is the reference, but the Python sink is narrower than the MCP
wire in two ways, and following the TS on either would silently lose the tool's
payload. Each divergent case below says which upstream function forces it.
"""

from __future__ import annotations

import json

import pytest

from computeragent.mcp.content import normalize_content


def text_of(blocks):
    return [b["text"] for b in blocks if b["type"] == "text"]


# ── shapes the TS and Python agree on ────────────────────────────────────────


@pytest.mark.parametrize("given", [None, [], {}.get("missing")])
def test_nothing_becomes_no_blocks(given):
    assert normalize_content(given) == []


def test_a_bare_string_becomes_one_text_block():
    assert normalize_content("hello") == [{"type": "text", "text": "hello"}]


@pytest.mark.parametrize(
    "block",
    [
        {"type": "text", "text": "hi"},
        {"type": "image", "data": "iVBOR", "mimeType": "image/png"},
        {"type": "resource_link", "uri": "file:///x", "name": "x"},
        {"type": "resource", "resource": {"text": "body", "uri": "file:///y"}},
    ],
)
def test_a_convertible_block_passes_through_untouched(block):
    assert normalize_content([block]) == [block]


def test_a_lone_object_is_treated_as_one_block():
    assert normalize_content({"type": "text", "text": "hi"}) == [{"type": "text", "text": "hi"}]


def test_an_unknown_type_is_stringified_rather_than_dropped():
    out = normalize_content([{"type": "diagram", "svg": "<svg/>"}])

    assert len(out) == 1
    assert json.loads(out[0]["text"])["svg"] == "<svg/>"


@pytest.mark.parametrize("element", [42, None, True, "loose"])
def test_a_non_object_element_is_stringified(element):
    assert normalize_content([element]) == [{"type": "text", "text": str(element)}]


def test_order_is_preserved_across_a_mixed_result():
    out = normalize_content(
        [{"type": "text", "text": "a"}, {"type": "diagram"}, {"type": "text", "text": "c"}]
    )

    assert [b["type"] for b in out] == ["text", "text", "text"]
    assert text_of(out)[0] == "a"
    assert text_of(out)[2] == "c"


def test_an_unserialisable_block_still_produces_a_block():
    """Whatever the tool returned must reach the model; dropping it is the one
    outcome this function exists to prevent."""

    class Odd:
        def __repr__(self):
            return "<odd>"

    out = normalize_content([{"type": "weird", "payload": Odd()}])

    assert len(out) == 1
    assert "odd" in out[0]["text"]


# ── the two deliberate divergences ───────────────────────────────────────────


def test_a_structured_block_stays_in_content_instead_of_being_hoisted():
    """DIVERGES from `mcp.ts:413`, which hoists it to `structuredContent`.

    `create_sdk_mcp_server`'s `run_tool` builds its CallToolResult from `content`
    and `isError` only, so `structuredContent` has no path to the CLI — hoisting
    here would delete the payload instead of relocating it.
    """
    out = normalize_content(
        [{"type": "text", "text": "summary"}, {"type": "structured", "data": {"k": 1}}]
    )

    assert len(out) == 2
    assert json.loads(out[1]["text"])["data"] == {"k": 1}


def test_a_second_structured_block_also_survives():
    """The TS keeps the first and silently drops the second, because it has one
    `structuredContent` slot. As text blocks we have no such limit, so there is
    no reason to lose either."""
    out = normalize_content(
        [{"type": "structured", "data": {"n": 1}}, {"type": "structured", "data": {"n": 2}}]
    )

    assert [json.loads(b["text"])["data"]["n"] for b in out] == [1, 2]


def test_an_audio_block_becomes_text_instead_of_passing_through():
    """DIVERGES from `mcp.ts:363`, whose pass-through set includes `audio`.

    `_convert_tool_content` handles text/image/resource_link/resource and drops
    everything else with a warning — so passing audio on would lose it.
    """
    out = normalize_content([{"type": "audio", "data": "SUQz", "mimeType": "audio/mpeg"}])

    assert out[0]["type"] == "text"
    assert json.loads(out[0]["text"])["data"] == "SUQz"


# ── logging ──────────────────────────────────────────────────────────────────


def test_coercion_is_logged_once_per_call_listing_the_distinct_types(caplog):
    """A facade that starts emitting a new unconvertible type is how this broke
    the first time, and it was invisible until a model reported a failure."""
    with caplog.at_level("WARNING", logger="computeragent.mcp.content"):
        normalize_content(
            [{"type": "structured", "data": {}}, {"type": "audio"}, {"type": "structured"}]
        )

    records = [r for r in caplog.records if "coerced" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "3" in message
    assert "audio" in message and "structured" in message


def test_nothing_is_logged_when_every_block_was_convertible(caplog):
    with caplog.at_level("WARNING", logger="computeragent.mcp.content"):
        normalize_content([{"type": "text", "text": "fine"}])

    assert not [r for r in caplog.records if "coerced" in r.getMessage()]


# ── the tripwire ─────────────────────────────────────────────────────────────


def test_upstreams_content_union_has_not_widened():
    """The two divergences above are true of the pinned claude-agent-sdk and
    could be fixed upstream. If `_convert_tool_content` ever starts keeping
    `audio` — or `run_tool` starts forwarding `structuredContent` — this file's
    expectations are the thing to revisit, deliberately, rather than discovering
    the drift years later.
    """
    import inspect

    import claude_agent_sdk

    convert = claude_agent_sdk._convert_tool_content
    kept = convert(
        [
            {"type": "text", "text": "t"},
            {"type": "audio", "data": "x", "mimeType": "audio/mpeg"},
        ]
    )
    assert [b["type"] for b in kept] == ["text"], (
        "claude_agent_sdk now converts `audio`; revisit CONVERTIBLE and the audio test"
    )

    source = inspect.getsource(claude_agent_sdk.create_sdk_mcp_server)
    assert "structuredContent" not in source, (
        "create_sdk_mcp_server now forwards structuredContent; hoisting may be correct again"
    )
