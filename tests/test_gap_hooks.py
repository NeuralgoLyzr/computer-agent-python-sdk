"""GAP hooks loader — ``hooks/hooks.yaml`` → the SDK ``hooks`` option."""

from __future__ import annotations

from pathlib import Path

import pytest

from computeragent.identity.gap_hooks import load_gap_hooks


def _write_hooks(tmp_path: Path, content: str) -> Path:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    (hooks_dir / "hooks.yaml").write_text(content)
    return tmp_path


def test_missing_file_yields_empty(tmp_path: Path):
    assert load_gap_hooks(tmp_path) == {}


def test_malformed_yaml_yields_empty(tmp_path: Path):
    _write_hooks(tmp_path, "{not yaml: [")
    assert load_gap_hooks(tmp_path) == {}


def test_matcher_and_seconds_timeout(tmp_path: Path):
    _write_hooks(
        tmp_path,
        "PreToolUse:\n"
        "  - matcher: Bash\n"
        "    command: ./hooks/check.sh\n"
        "    timeout_ms: 5000\n"
        "  - command: ./hooks/all.sh\n",
    )
    out = load_gap_hooks(tmp_path)
    assert set(out) == {"PreToolUse"}
    first, second = out["PreToolUse"]
    assert first.matcher == "Bash"
    assert first.timeout == 5  # ms → seconds, ceil
    assert len(first.hooks) == 1
    assert second.matcher is None
    assert second.timeout is None


def test_unknown_event_forwarded(tmp_path: Path):
    _write_hooks(tmp_path, "SessionStart:\n  - command: 'echo hi'\n")
    out = load_gap_hooks(tmp_path)
    assert "SessionStart" in out  # not validated; the CLI decides


def test_entries_missing_command_skipped(tmp_path: Path):
    _write_hooks(tmp_path, "PreToolUse:\n  - matcher: Bash\n  - command: ''\n")
    assert load_gap_hooks(tmp_path) == {}


@pytest.mark.asyncio
async def test_callback_stdout_json_with_key_remap(tmp_path: Path):
    # The hook echoes JSON using the WIRE names; the loader must remap
    # `continue` → `continue_` and `async` → `async_` for the Python SDK.
    _write_hooks(
        tmp_path,
        "PreToolUse:\n"
        """  - command: "cat > /dev/null; printf '{\\"continue\\": false, \\"systemMessage\\": \\"halt\\"}'"\n""",
    )
    out = load_gap_hooks(tmp_path)
    cb = out["PreToolUse"][0].hooks[0]
    result = await cb({"tool_name": "Bash"}, None, None)
    assert result == {"continue_": False, "systemMessage": "halt"}


@pytest.mark.asyncio
async def test_callback_nonzero_exit_is_system_message_not_deny(tmp_path: Path):
    _write_hooks(
        tmp_path,
        "PreToolUse:\n"
        """  - command: "cat > /dev/null; echo oops >&2; exit 3"\n""",
    )
    cb = load_gap_hooks(tmp_path)["PreToolUse"][0].hooks[0]
    result = await cb({}, None, None)
    assert "systemMessage" in result
    assert "exited 3" in result["systemMessage"]
    assert "oops" in result["systemMessage"]
    assert "hookSpecificOutput" not in result  # non-zero exit does NOT deny


@pytest.mark.asyncio
async def test_callback_empty_or_bad_stdout_is_empty_dict(tmp_path: Path):
    _write_hooks(
        tmp_path,
        "PostToolUse:\n"
        """  - command: "cat > /dev/null; echo 'not json'"\n""",
    )
    cb = load_gap_hooks(tmp_path)["PostToolUse"][0].hooks[0]
    assert await cb({}, None, None) == {}


@pytest.mark.asyncio
async def test_callback_timeout_kills_and_reports_124(tmp_path: Path):
    _write_hooks(
        tmp_path,
        "PreToolUse:\n"
        "  - command: 'sleep 10'\n"
        "    timeout_ms: 300\n",
    )
    cb = load_gap_hooks(tmp_path)["PreToolUse"][0].hooks[0]
    result = await cb({}, None, None)
    assert "exited 124" in result["systemMessage"]
