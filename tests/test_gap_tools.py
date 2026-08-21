"""GAP tools loader — ``tools/*.yaml`` → builtin aliases + in-process MCP."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from computeragent.identity.gap_tools import (
    MCP_SERVER_NAME,
    _build_script_tool,
    load_gap_tools,
)


def test_missing_tools_dir(tmp_path: Path):
    result = load_gap_tools(tmp_path)
    assert result.allowed_tools == []
    assert result.mcp_server is None
    assert result.mcp_tool_names == []


def test_builtin_alias_lands_on_allowed_tools(tmp_path: Path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "reader.yaml").write_text(
        "name: reader\nimplementation:\n  builtin: Read\n"
    )
    result = load_gap_tools(tmp_path)
    assert result.allowed_tools == ["Read"]
    assert result.mcp_server is None  # no script tools → no server


def test_script_tool_builds_mcp_server(tmp_path: Path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "hello.yaml").write_text(
        "name: hello\n"
        "description: Says hello\n"
        "parameters:\n"
        "  type: object\n"
        "  properties:\n"
        "    msg: {type: string}\n"
        "  required: [msg]\n"
        "implementation:\n"
        "  script:\n"
        "    command: echo\n"
    )
    result = load_gap_tools(tmp_path)
    assert result.mcp_tool_names == [f"mcp__{MCP_SERVER_NAME}__hello"]
    assert result.mcp_server is not None
    assert result.mcp_server["type"] == "sdk"
    assert result.mcp_server["name"] == MCP_SERVER_NAME


def test_invalid_manifests_silently_skipped(tmp_path: Path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "no-name.yaml").write_text("implementation:\n  builtin: Read\n")
    (tools / "no-impl.yaml").write_text("name: x\n")
    (tools / "no-command.yaml").write_text(
        "name: y\nimplementation:\n  script: {args: [a]}\n"
    )
    (tools / "broken.yaml").write_text("{not yaml: [")
    (tools / "notes.md").write_text("ignored — not yaml")
    result = load_gap_tools(tmp_path)
    assert result.allowed_tools == []
    assert result.mcp_server is None


def _script_manifest(command: str, args: list[str], **script_extra) -> dict:
    return {
        "name": "t",
        "description": "test tool",
        "parameters": {"type": "object", "properties": {"msg": {"type": "string"}}},
        "implementation": {"script": {"command": command, "args": args, **script_extra}},
    }


@pytest.mark.asyncio
async def test_script_handler_executes_with_stdin_and_env(tmp_path: Path):
    # Reads the JSON args from stdin AND the upper-cased arg from env — both
    # injection paths the TS loader guarantees.
    code = (
        "import sys, os, json; "
        "data = json.load(sys.stdin); "
        "print(data['msg'] + '|' + os.environ.get('MSG', '?'))"
    )
    sdk_tool = _build_script_tool(_script_manifest(sys.executable, ["-c", code]), tmp_path)
    out = await sdk_tool.handler({"msg": "hi"})
    assert "is_error" not in out
    assert out["content"][0]["text"].strip() == "hi|hi"


@pytest.mark.asyncio
async def test_script_nonzero_exit_is_error(tmp_path: Path):
    code = "import sys; sys.stderr.write('boom'); sys.exit(2)"
    sdk_tool = _build_script_tool(_script_manifest(sys.executable, ["-c", code]), tmp_path)
    out = await sdk_tool.handler({})
    assert out["is_error"] is True
    assert out["content"][0]["text"].startswith("exit 2:")
    assert "boom" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_script_timeout_returns_124(tmp_path: Path):
    code = "import time; time.sleep(10)"
    sdk_tool = _build_script_tool(
        _script_manifest(sys.executable, ["-c", code], timeout_ms=300), tmp_path
    )
    out = await sdk_tool.handler({})
    assert out["is_error"] is True
    assert out["content"][0]["text"].startswith("exit 124:")


@pytest.mark.asyncio
async def test_script_missing_binary_returns_127(tmp_path: Path):
    sdk_tool = _build_script_tool(
        _script_manifest("/definitely/not/a/binary", []), tmp_path
    )
    out = await sdk_tool.handler({})
    assert out["is_error"] is True
    assert out["content"][0]["text"].startswith("exit 127:")


def test_raw_json_schema_passthrough(tmp_path: Path):
    sdk_tool = _build_script_tool(
        {
            "name": "s",
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "integer", "description": "A"}},
                "required": ["a"],
            },
            "implementation": {"script": {"command": "echo"}},
        },
        tmp_path,
    )
    # The declared JSON Schema rides through verbatim (type+properties trigger
    # the SDK's raw-schema path — no zod-style rebuild).
    assert sdk_tool.input_schema == {
        "type": "object",
        "properties": {"a": {"type": "integer", "description": "A"}},
        "required": ["a"],
    }
