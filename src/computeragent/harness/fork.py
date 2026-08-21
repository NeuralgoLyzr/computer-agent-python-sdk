"""Process-level agent forking — the ``mcp__fork__spawn`` tool.

Additive, opt-in (``ComputerAgent(fork_mode=...)``; OFF by default). Lets a
running agent spawn a **child** ``ComputerAgent`` — a *separate* Claude Code
process with its own workspace and its own ``ca_…`` session — instead of the
Claude CLI's in-process Task sub-agent. Because a fork is a real
``ComputerAgent``, each child gets its own ``agent_id`` / ``agent_name`` /
``session_id`` (linked back to the parent via ``parent_session_id`` /
``parent_agent_id``), so sub-agents finally show up as first-class,
independently observable + guardrail-able agents.

Mechanism (mirrors ``identity/gap_tools.py``): an in-process MCP server named
``fork`` exposing one async tool ``spawn``. The handler builds a child
``ComputerAgent`` and ``await``s its full run — the SDK dispatches each tool
call as its own detached task, so a long child run doesn't block the parent.

Two ways to trigger it (both use this same tool):
* **manual** — the agent calls ``mcp__fork__spawn`` itself.
* **auto** — the coordinator disables the native ``agents``/``Task`` tool and
  steers delegation here via a system-prompt line (see ``append_steer``).

Safety (all net-new — the SDK has no such guards): ``max_depth`` (a child may
itself fork only while under the cap), ``max_children`` (lifetime fan-out
cap), ``max_concurrency`` (a semaphore), a per-child ``asyncio.wait_for``
timeout, and guaranteed teardown of temp workspaces + child processes on the
parent's ``dispose``.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

if TYPE_CHECKING:  # avoid a construction-time import cycle
    from .coordinator import HarnessCoordinator

logger = logging.getLogger("computeragent.harness.fork")

FORK_SERVER_NAME = "fork"
FORK_TOOL_NAME = "spawn"
FORK_TOOL_QUALIFIED = f"mcp__{FORK_SERVER_NAME}__{FORK_TOOL_NAME}"

# Appended to the system prompt in AUTO mode so the model delegates by
# spawning a separate process rather than reaching for in-process Task.
_STEER_TEXT = (
    "\n\n## Delegation\n"
    "To delegate a subtask, spawn a SEPARATE agent process by calling the "
    f"`{FORK_TOOL_QUALIFIED}` tool with a `task` (and optionally an `agent` "
    "preset name, or a `name`+`role` to define a fresh specialist, plus "
    "`model`/`tools`). Each spawn is an isolated agent with its own session. "
    "Do NOT use in-process sub-agent delegation."
)


@dataclass
class ForkConfig:
    """Tuning + safety limits for process-level forking.

    ``allowed_presets=None`` means "any ``agents/<name>`` defined in the
    parent's workspace"; pass a list to restrict which presets may be forked.
    Dynamic (``name``+``role``) children are always allowed — they carry no
    remote code. External path/git-URL sources are intentionally NOT forkable
    in this pass (that would be clone-and-run; needs an allowlist).
    """

    max_depth: int = 2
    max_children: int = 8
    max_concurrency: int = 4
    child_timeout_s: float = 300.0
    inherit_workspace: bool = False
    inherit_options: bool = True  # inherit model + permission_mode from parent
    allowed_presets: list[str] | None = None


class ForkContext:
    """Per-parent-coordinator fork state.

    Holds a reference to the parent coordinator and reads its identity
    LAZILY (the parent ``session_id`` is minted at first chat, after the fork
    server is built), enforces the fan-out/concurrency caps, and tracks live
    children so the parent's ``dispose`` can tear them down.
    """

    def __init__(
        self,
        coordinator: HarnessCoordinator,
        config: ForkConfig,
        depth: int,
        mode: str,
    ) -> None:
        self._coord = coordinator
        self.config = config
        self.depth = depth
        self.mode = mode
        self._sem = asyncio.Semaphore(max(1, config.max_concurrency))
        self._count = 0
        self._count_lock = asyncio.Lock()
        self._live: set[Any] = set()

    # ── lazy parent identity/config reads ────────────────────────────────
    @property
    def parent_session_id(self) -> str | None:
        return self._coord._lifecycle_session_id

    @property
    def parent_agent_id(self) -> str | None:
        return self._coord._agent_id

    @property
    def parent_agent_name(self) -> str | None:
        m = getattr(self._coord, "_manifest", None)
        return self._coord._agent_name or (m.name if m is not None else None)

    @property
    def parent_pipeline(self) -> Any:
        return self._coord._pipeline

    @property
    def parent_engine_name(self) -> str:
        return getattr(self._coord._engine, "name", "claude-agent-sdk")

    @property
    def parent_envs(self) -> dict[str, str]:
        return dict(self._coord._envs or {})

    @property
    def parent_workspace(self) -> Path | None:
        return self._coord._workspace_path

    @property
    def parent_options(self) -> dict[str, Any]:
        m = getattr(self._coord, "_manifest", None)
        return dict(m.options or {}) if m is not None else {}

    def resolve_preset(self, name: str) -> dict[str, Any] | None:
        """Read the parent's ``agents/<name>`` AgentDefinition into a spec."""
        ws = self.parent_workspace
        if ws is None:
            return None
        try:
            from ..identity.gap_subagents import load_gap_subagents

            agents = load_gap_subagents(ws)
        except Exception:  # noqa: BLE001
            return None
        d = agents.get(name)
        if d is None:
            return None
        return {
            "role": getattr(d, "prompt", None),
            "model": getattr(d, "model", None),
            "tools": getattr(d, "tools", None),
        }

    async def teardown(self) -> None:
        """Dispose any still-running children (parent dispose/cancel)."""
        for child in list(self._live):
            with suppress(Exception):
                await child.dispose()
            self._live.discard(child)


def append_steer(system_prompt: Any) -> Any:
    """Append the AUTO-mode delegation steer to a system-prompt of any shape."""
    if isinstance(system_prompt, dict):
        out = dict(system_prompt)
        out["append"] = (out.get("append") or "") + _STEER_TEXT
        return out
    if isinstance(system_prompt, str):
        return system_prompt + _STEER_TEXT
    # None / absent → augment the claude_code preset (don't replace it).
    return {"type": "preset", "preset": "claude_code", "append": _STEER_TEXT}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _result_text(result: Any) -> str:
    """Deep-walk a ChatResult's messages for human-readable text."""
    out: list[str] = []

    def walk(v: Any) -> None:
        if v is None:
            return
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        else:
            for attr in ("text", "content", "result"):
                if hasattr(v, attr):
                    walk(getattr(v, attr))

    walk(getattr(result, "messages", None))
    joined = "\n".join(s for s in out if s and s.strip())
    return joined[:8000]


async def _await_handle(handle: Any) -> Any:
    return await handle


def build_fork_mcp_server(ctx: ForkContext) -> tuple[Any, list[str]]:
    """Build the in-process ``fork`` MCP server + its qualified tool names.

    Mirrors ``gap_tools.load_gap_tools`` — one ``create_sdk_mcp_server`` whose
    dict key (registered by the coordinator as ``mcp_servers["fork"]``) equals
    the server name so the ``mcp__fork__spawn`` prefix resolves.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The subtask / prompt to hand the child agent.",
            },
            "agent": {
                "type": "string",
                "description": (
                    "Optional: name of a predefined sub-agent (agents/<name> in "
                    "this repo) to run as a separate child process."
                ),
            },
            "name": {
                "type": "string",
                "description": "Optional: a label for a dynamically-defined child.",
            },
            "role": {
                "type": "string",
                "description": (
                    "Optional: the system prompt / role for a dynamically-defined "
                    "child (used when `agent` is not given)."
                ),
            },
            "model": {"type": "string", "description": "Optional: model override for the child."},
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: tool allowlist for the child.",
            },
        },
        "required": ["task"],
    }

    description = (
        "Spawn a SEPARATE agent process (its own isolated session) to handle a "
        "subtask, then return its result. Use for delegating work to an "
        "independent specialist. Provide `task`, and either an `agent` preset "
        "name or a `name`+`role` to define one on the fly."
    )

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        return await _spawn(ctx, args or {})

    server = create_sdk_mcp_server(
        name=FORK_SERVER_NAME, version="0.1.0", tools=[tool(FORK_TOOL_NAME, description, schema)(handler)]
    )
    return server, [FORK_TOOL_QUALIFIED]


async def _spawn(ctx: ForkContext, args: dict[str, Any]) -> dict[str, Any]:
    task = args.get("task")
    if not (isinstance(task, str) and task.strip()):
        return _err("fork.spawn: `task` is required and must be a non-empty string.")

    # ── caps ──────────────────────────────────────────────────────────────
    if ctx.depth >= ctx.config.max_depth:
        return _err(
            f"fork.spawn refused: max fork depth ({ctx.config.max_depth}) reached — "
            "this agent is already a nested fork and cannot spawn further."
        )
    async with ctx._count_lock:
        if ctx._count >= ctx.config.max_children:
            return _err(
                f"fork.spawn refused: max children ({ctx.config.max_children}) already "
                "spawned by this agent."
            )
        ctx._count += 1

    # ── resolve the child spec ────────────────────────────────────────────
    preset_name = args.get("agent")
    if isinstance(preset_name, str) and preset_name.strip():
        if ctx.config.allowed_presets is not None and preset_name not in ctx.config.allowed_presets:
            return _err(
                f"fork.spawn refused: preset {preset_name!r} is not in the allowed set "
                f"{ctx.config.allowed_presets!r}."
            )
        spec = ctx.resolve_preset(preset_name)
        if spec is None:
            return _err(
                f"fork.spawn: no sub-agent preset named {preset_name!r} found in agents/. "
                "Define it, or pass `name`+`role` for a dynamic child."
            )
        child_name = preset_name
        role = spec.get("role")
        model = args.get("model") or spec.get("model")
        tools = args.get("tools") or spec.get("tools")
    else:
        child_name = (args.get("name") or "fork").strip() or "fork"
        role = args.get("role")
        model = args.get("model")
        tools = args.get("tools")

    # ── child identity + linkage ──────────────────────────────────────────
    parent_id = ctx.parent_agent_id or ctx.parent_agent_name or "agent"
    child_agent_id = f"{parent_id}/{child_name}#{uuid.uuid4().hex[:6]}"

    # ── child options (role/model/tools + inherited permission_mode) ──────
    child_options: dict[str, Any] = {}
    if isinstance(role, str) and role.strip():
        child_options["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": role,
        }
    if isinstance(model, str) and model:
        child_options["model"] = model
    if isinstance(tools, list) and tools:
        # An allowlist RESTRICTS the child to exactly these tools — only set it
        # when the caller/preset specified tools (else leave all tools available).
        child_options["allowed_tools"] = [t for t in tools if isinstance(t, str)]
    if ctx.config.inherit_options:
        pm = ctx.parent_options.get("permission_mode")
        if pm is not None:
            child_options.setdefault("permission_mode", pm)
        if "model" not in child_options and isinstance(ctx.parent_options.get("model"), str):
            child_options["model"] = ctx.parent_options["model"]

    # ── child workspace: fresh isolated temp dir (default) or parent's ────
    tmp_workspace: Path | None = None
    if ctx.config.inherit_workspace and ctx.parent_workspace is not None:
        child_source: Any = ctx.parent_workspace
    else:
        tmp_workspace = Path(tempfile.mkdtemp(prefix="computeragent-fork-"))
        child_source = tmp_workspace

    child_envs = ctx.parent_envs
    child_envs["COMPUTERAGENT_FORK_DEPTH"] = str(ctx.depth + 1)

    child_fork_mode = ctx.mode if (ctx.depth + 1) < ctx.config.max_depth else "off"

    from . import ComputerAgent  # deferred — avoids the construction import cycle

    child: Any = None
    async with ctx._sem:
        try:
            child = ComputerAgent(
                source=child_source,
                engine=ctx.parent_engine_name,
                identity_loader="passthrough",
                agent_id=child_agent_id,
                agent_name=child_name,
                options=child_options,
                telemetry_pipeline=ctx.parent_pipeline,
                envs=child_envs,
                parent_session_id=ctx.parent_session_id,
                parent_agent_id=ctx.parent_agent_id,
                fork_mode=child_fork_mode,
                fork_config=ctx.config,
                _fork_depth=ctx.depth + 1,
            )
        except Exception as exc:  # noqa: BLE001
            if tmp_workspace is not None:
                shutil.rmtree(tmp_workspace, ignore_errors=True)
            return _err(f"fork.spawn: failed to construct child agent: {type(exc).__name__}: {exc}")

        ctx._live.add(child)
        try:
            handle = child.chat(task)
            result = await asyncio.wait_for(_await_handle(handle), timeout=ctx.config.child_timeout_s)
            text = _result_text(result)
            child_session = getattr(result, "session_id", None)
            usage = getattr(result, "usage", None)
            cost = getattr(usage, "cost_usd", None) if usage is not None else None
            header = (
                f"[fork child agent_id={child_agent_id} session={child_session} "
                f"cost=${cost}]\n"
            )
            return {"content": [{"type": "text", "text": header + (text or "(no output)")}]}
        except asyncio.TimeoutError:
            return _err(
                f"fork.spawn: child {child_agent_id} exceeded the "
                f"{ctx.config.child_timeout_s:.0f}s timeout and was cancelled."
            )
        except Exception as exc:  # noqa: BLE001
            return _err(f"fork.spawn: child {child_agent_id} failed: {type(exc).__name__}: {exc}")
        finally:
            ctx._live.discard(child)
            with suppress(Exception):
                await child.dispose()
            if tmp_workspace is not None:
                shutil.rmtree(tmp_workspace, ignore_errors=True)


__all__ = ["ForkConfig", "ForkContext", "build_fork_mcp_server", "append_steer", "FORK_TOOL_QUALIFIED"]
