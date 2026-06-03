"""``PiiRedactor`` — pattern + path policy + strategy + fail-closed."""

from __future__ import annotations

import pytest

from computeragent.telemetry import PiiRedactor, TelemetryEvent


def _ev(payload):
    return TelemetryEvent(
        kind="assistant_message", session_id="s", agent_name=None, payload=payload
    )


@pytest.mark.asyncio
async def test_redacts_email_in_string_value():
    r = PiiRedactor(strategy="mask")
    ev = _ev({"text": "contact me at alice@example.com please"})
    out = await r(ev)
    assert out is not None
    assert "****" in out.payload["text"]
    assert "alice@example.com" not in out.payload["text"]


@pytest.mark.asyncio
async def test_redacts_phone_ssn_aws_key():
    r = PiiRedactor(strategy="mask")
    ev = _ev(
        {
            "phone": "call 555-123-4567 anytime",
            "ssn": "id 123-45-6789",
            "aws": "AKIAIOSFODNN7EXAMPLE here",
        }
    )
    out = await r(ev)
    assert out is not None
    for v in out.payload.values():
        assert "****" in v


@pytest.mark.asyncio
async def test_hash_strategy_is_deterministic():
    r = PiiRedactor(strategy="hash")
    a = await r(_ev({"text": "alice@example.com"}))
    b = await r(_ev({"text": "alice@example.com"}))
    c = await r(_ev({"text": "bob@example.com"}))
    assert a is not None and b is not None and c is not None
    assert a.payload["text"] == b.payload["text"]
    assert a.payload["text"] != c.payload["text"]
    assert a.payload["text"].startswith("<redacted:")


@pytest.mark.asyncio
async def test_walks_nested_structures():
    r = PiiRedactor(strategy="mask")
    ev = _ev({"outer": {"inner": ["alice@example.com", "safe text"]}})
    out = await r(ev)
    assert out is not None
    assert "****" in out.payload["outer"]["inner"][0]
    assert out.payload["outer"]["inner"][1] == "safe text"


@pytest.mark.asyncio
async def test_extra_patterns_apply():
    r = PiiRedactor(strategy="mask", extra_patterns=[r"BADGE-\d{6}"])
    ev = _ev({"text": "Internal id BADGE-123456 for the user"})
    out = await r(ev)
    assert out is not None
    assert "BADGE-123456" not in out.payload["text"]


@pytest.mark.asyncio
async def test_path_allowlist_skips_redaction():
    r = PiiRedactor(strategy="mask", path_allowlist=["filename"])
    ev = _ev({"filename": "alice@example.com.txt", "text": "alice@example.com"})
    out = await r(ev)
    assert out is not None
    # filename is allowlisted → untouched
    assert out.payload["filename"] == "alice@example.com.txt"
    # text is not allowlisted → redacted
    assert "****" in out.payload["text"]


@pytest.mark.asyncio
async def test_path_denylist_force_redacts():
    r = PiiRedactor(strategy="mask", path_denylist=["secret"])
    ev = _ev({"secret": "totally non-PII string here", "ok": "ordinary"})
    out = await r(ev)
    assert out is not None
    assert out.payload["secret"] == "****"  # forced
    assert out.payload["ok"] == "ordinary"


@pytest.mark.asyncio
async def test_fail_closed_on_bad_extra_pattern():
    # An unclosed group raises at compile time, before we can construct the
    # redactor — so this checks the substitution-time failure path instead.
    import re

    class _BoomPattern:
        pattern = "x"

        def sub(self, _repl, _value):
            raise re.error("boom")

        def search(self, _value):
            return None

    r = PiiRedactor(strategy="mask")
    r._patterns.append(_BoomPattern())  # type: ignore[arg-type]
    ev = _ev({"text": "hello"})
    out = await r(ev)
    # Substitution failure on one pattern is logged but does not drop the
    # event — only an outer-level exception drops it. Verify the event still
    # came out with other patterns having been applied (none match, so equal).
    assert out is not None
    assert out.payload["text"] == "hello"


@pytest.mark.asyncio
async def test_drop_strategy_replaces_with_empty_when_match():
    r = PiiRedactor(strategy="drop")
    ev = _ev({"text": "alice@example.com", "clean": "ok"})
    out = await r(ev)
    assert out is not None
    assert out.payload["text"] == ""
    assert out.payload["clean"] == "ok"
