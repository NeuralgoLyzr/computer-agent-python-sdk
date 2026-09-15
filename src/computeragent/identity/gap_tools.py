"""GAP tools translator — Python port of the TS ``tools.ts``.

Reads ``<workdir>/tools/*.yaml`` (non-recursive) and splits the manifests:

* ``implementation.builtin`` — an alias to a Claude built-in tool (e.g.
  ``Read``); the bare name lands on ``allowed_tools``.
* ``implementation.script`` — a script tool; wrapped into the in-process MCP
  server ``gap_tools`` (``create_sdk_mcp_server``), invokable as
  ``mcp__gap_tools__<name>``.

Tool manifest shape (unknown keys ignored; invalid manifests silently
skipped — same fail-soft posture as the TS loader)::

    name: <required>
    description: <optional>
    parameters:            # JSON-Schema subset, passed to the SDK VERBATIM
      type: object
      properties: { ... }
      required: [ ... ]
    implementation:
      builtin: Read        # either this...
      script:              # ...or this (builtin wins if both)
        command: ./tools/run.sh
        args: [--flag]
        cwd: subdir        # relative to the workdir
        env: { KEY: val }
        timeout_ms: 30000

Script execution semantics (mirror TS exactly):

* spawned WITHOUT a shell (:func:`asyncio.create_subprocess_exec`);
* env = ``os.environ`` ⊕ ``script.env`` ⊕ every call arg upper-cased
  (strings verbatim, ``None`` → ``""``, else JSON-encoded);
* the full args object is written to **stdin** as JSON;
* timeout (default 30s) → kill → exit code 124; spawn failure → 127;
* non-zero exit → ``is_error`` text ``exit <code>: <stderr[:800]>``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

logger = logging.getLogger("computeragent.identity.gap")

MCP_SERVER_NAME = "gap_tools"

_DEFAULT_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class GapToolsResult:
    """What ``load_gap_tools`` hands the options builder.

    ``allowed_tools`` are bare built-in aliases; ``mcp_tool_names`` are the
    fully-qualified ``mcp__gap_tools__<name>`` strings — both belong on the
    SDK's ``allowed_tools``. ``mcp_server`` (when script tools exist) must be
    registered as ``mcp_servers["gap_tools"]`` — the dict key must equal the
    server name for the ``mcp__gap_tools__*`` prefix to resolve.
    """

    allowed_tools: list[str] = field(default_factory=list)
    mcp_server: Any | None = None
    mcp_tool_names: list[str] = field(default_factory=list)


def load_gap_tools(workdir: Path) -> GapToolsResult:
    tools_dir = workdir / "tools"
    try:
        entries = sorted(tools_dir.iterdir())
    except OSError:
        return GapToolsResult()

    allowed: list[str] = []
    script_tools: list[dict[str, Any]] = []

    for entry in entries:
        if not (entry.is_file() and entry.suffix in (".yaml", ".yml")):
            continue
        manifest = _parse_tool_manifest(entry)
        if manifest is None:
            continue
        impl = manifest["implementation"]
        builtin = impl.get("builtin")
        if isinstance(builtin, str) and builtin:
            allowed.append(builtin)
            continue
        if isinstance(impl.get("script"), dict):
            script_tools.append(manifest)

    if not script_tools:
        return GapToolsResult(allowed_tools=allowed)

    sdk_tools = [_build_script_tool(t, workdir) for t in script_tools]
    server = create_sdk_mcp_server(name=MCP_SERVER_NAME, version="0.1.0", tools=sdk_tools)
    mcp_names = [f"mcp__{MCP_SERVER_NAME}__{t['name']}" for t in script_tools]
    return GapToolsResult(allowed_tools=allowed, mcp_server=server, mcp_tool_names=mcp_names)


# ── manifest parsing ───────────────────────────────────────────────────


def _parse_tool_manifest(path: Path) -> dict[str, Any] | None:
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except Exception:  # noqa: BLE001 — unreadable/malformed → skip
        return None
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    impl = raw.get("implementation")
    if not (isinstance(name, str) and name.strip()):
        return None
    if not isinstance(impl, dict):
        return None
    script = impl.get("script")
    if script is not None:
        if not isinstance(script, dict):
            return None
        command = script.get("command")
        if not (isinstance(command, str) and command.strip()):
            return None
    return raw


# ── script tool construction ───────────────────────────────────────────


def _build_script_tool(manifest: dict[str, Any], workdir: Path) -> Any:
    """One GAP script manifest → an ``SdkMcpTool`` (via the ``tool()`` factory).

    Built through a dedicated function (not inline in the loop) so each
    handler closes over ITS manifest — the classic late-binding trap.
    """
    name: str = manifest["name"]
    description: str = manifest.get("description") or f"GAP tool: {name}"
    script: dict[str, Any] = manifest["implementation"]["script"]
    schema = _input_schema(manifest.get("parameters"))

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        code, stdout, stderr = await _run_script(script, workdir, args or {})
        if code != 0:
            return {
                "content": [{"type": "text", "text": f"exit {code}: {stderr[:800]}"}],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": stdout}]}

    return tool(name, description, schema)(handler)


def _input_schema(parameters: Any) -> dict[str, Any]:
    """GAP ``parameters`` → the SDK's ``input_schema``.

    The Python SDK accepts a raw JSON-Schema dict verbatim when it carries
    both ``type`` and ``properties`` — so unlike the TS loader (which
    rebuilds a zod shape) we pass the declared schema straight through,
    normalising only the two keys the passthrough requires.
    """
    props: dict[str, Any] = {}
    required: list[str] = []
    if isinstance(parameters, dict):
        if isinstance(parameters.get("properties"), dict):
            props = parameters["properties"]
        if isinstance(parameters.get("required"), list):
            required = [r for r in parameters["required"] if isinstance(r, str)]
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


async def _run_script(
    script: dict[str, Any], workdir: Path, args: dict[str, Any]
) -> tuple[int, str, str]:
    command: str = script["command"]
    argv: list[str] = [a for a in (script.get("args") or []) if isinstance(a, str)]
    cwd = workdir / script["cwd"] if isinstance(script.get("cwd"), str) else workdir
    timeout_ms = script.get("timeout_ms")
    timeout_s = (
        timeout_ms if isinstance(timeout_ms, int) and timeout_ms > 0 else _DEFAULT_TIMEOUT_MS
    ) / 1000

    env = dict(os.environ)
    extra_env = script.get("env")
    if isinstance(extra_env, dict):
        env.update({str(k): str(v) for k, v in extra_env.items()})
    # Every call arg is ALSO exposed upper-cased in the env (TS parity).
    for key, value in args.items():
        env[str(key).upper()] = _stringify(value)

    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            *argv,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception:  # noqa: BLE001 — missing binary / spawn failure
        return 127, "", f"failed to spawn: {command}"

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=json.dumps(args).encode("utf-8")),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        with suppress(Exception):
            proc.kill()
        with suppress(Exception):
            await proc.wait()
        return 124, "", f"timed out after {int(timeout_s * 1000)}ms"
    except (BrokenPipeError, ConnectionResetError):
        # EPIPE on stdin (script exited before reading) — TS swallows this.
        await proc.wait()
        return proc.returncode or 0, "", ""

    return (
        proc.returncode or 0,
        stdout_b.decode("utf-8", "replace"),
        stderr_b.decode("utf-8", "replace"),
    )


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value)


__all__ = ["GapToolsResult", "load_gap_tools", "MCP_SERVER_NAME"]
