"""GAP hooks translator — Python port of the TS ``hooks.ts``.

Reads exactly one file, ``<workdir>/hooks/hooks.yaml``, and produces the
Claude Agent SDK's ``hooks`` option shape
(``dict[HookEvent, list[HookMatcher]]``). Missing / malformed → ``{}``.

File shape — top-level keys are hook event names (NOT validated against the
SDK's event union: unknown events are forwarded and the CLI ignores them,
same as the TS loader)::

    PreToolUse:
      - matcher: "Bash"
        command: ./hooks/check-bash.sh
        timeout_ms: 5000
    PostToolUse:
      - command: ./hooks/log.sh

Each YAML entry becomes one ``HookMatcher`` with exactly one callback;
``timeout_ms`` is converted to the matcher's ``timeout`` in **seconds**
(``ceil``). Callback semantics (mirror TS):

* spawned WITH a shell (``create_subprocess_shell`` — hooks are commands,
  contrast tools which are exec'd), cwd = the workdir;
* the SDK's hook input dict is written to stdin as JSON;
* timeout (default 30s) → kill → exit 124; spawn failure → 127. The SDK's
  ``HookContext.signal`` is always ``None`` in 0.2.x, so the timeout is the
  only bound;
* non-zero exit → ``{"systemMessage": ...}`` — it does NOT deny; a deny must
  come from stdout ``hookSpecificOutput.permissionDecision: "deny"``;
* exit 0 → stdout parsed as JSON (empty/unparsable → ``{}``), with the
  top-level keys ``continue`` → ``continue_`` and ``async`` → ``async_``
  remapped (the SDK converts the underscore names back on the wire).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from contextlib import suppress
from pathlib import Path
from typing import Any

from claude_agent_sdk.types import HookMatcher

logger = logging.getLogger("computeragent.identity.gap")

_DEFAULT_TIMEOUT_MS = 30_000
_STDOUT_KEY_REMAP = {"continue": "continue_", "async": "async_"}


def load_gap_hooks(workdir: Path) -> dict[str, list[HookMatcher]]:
    """Read ``<workdir>/hooks/hooks.yaml`` into the SDK hooks map."""
    raw = _parse_yaml_file(workdir / "hooks" / "hooks.yaml")
    if not isinstance(raw, dict):
        return {}

    out: dict[str, list[HookMatcher]] = {}
    for event, entries in raw.items():
        if not (isinstance(event, str) and isinstance(entries, list)):
            continue
        matchers: list[HookMatcher] = []
        for entry in entries:
            matcher = _build_matcher(entry, workdir)
            if matcher is not None:
                matchers.append(matcher)
        if matchers:
            out[event] = matchers
    return out


def _build_matcher(entry: Any, workdir: Path) -> HookMatcher | None:
    if not isinstance(entry, dict):
        return None
    command = entry.get("command")
    if not (isinstance(command, str) and command.strip()):
        return None
    timeout_ms = entry.get("timeout_ms")
    if not (isinstance(timeout_ms, int) and timeout_ms > 0):
        timeout_ms = None

    callback = _build_callback(command, workdir, timeout_ms or _DEFAULT_TIMEOUT_MS)
    matcher = HookMatcher(hooks=[callback])
    if isinstance(entry.get("matcher"), str):
        matcher.matcher = entry["matcher"]
    if timeout_ms is not None:
        matcher.timeout = math.ceil(timeout_ms / 1000)
    return matcher


def _build_callback(command: str, workdir: Path, timeout_ms: int) -> Any:
    """One hooks.yaml entry → an SDK ``HookCallback``.

    Built in its own function so the closure binds THIS entry's command
    (late-binding trap when constructed in a loop).
    """

    async def callback(hook_input: Any, _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        code, stdout, stderr = await _run_hook(command, workdir, hook_input, timeout_ms)
        if code != 0:
            return {"systemMessage": f"hook '{command}' exited {code}: {stderr[:400]}"}
        return _parse_hook_output(stdout)

    return callback


async def _run_hook(
    command: str, workdir: Path, hook_input: Any, timeout_ms: int
) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(workdir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception:  # noqa: BLE001 — spawn failure
        return 127, "", f"failed to spawn: {command}"

    try:
        payload = json.dumps(hook_input, default=str).encode("utf-8")
    except (TypeError, ValueError):
        payload = b"{}"

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=payload), timeout=timeout_ms / 1000
        )
    except asyncio.TimeoutError:
        with suppress(Exception):
            proc.kill()
        with suppress(Exception):
            await proc.wait()
        return 124, "", f"timed out after {timeout_ms}ms"
    except (BrokenPipeError, ConnectionResetError):
        await proc.wait()
        return proc.returncode or 0, "", ""

    return proc.returncode or 0, stdout_b.decode("utf-8", "replace"), stderr_b.decode("utf-8", "replace")


def _parse_hook_output(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {_STDOUT_KEY_REMAP.get(k, k): v for k, v in parsed.items()}


def _parse_yaml_file(path: Path) -> Any:
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except Exception:  # noqa: BLE001 — unreadable/malformed → {}
        return None


__all__ = ["load_gap_hooks"]
