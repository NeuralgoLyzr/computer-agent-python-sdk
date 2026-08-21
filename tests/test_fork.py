"""Unit coverage for process-level agent forking (harness/fork.py + the
coordinator wiring). Deterministic — no real Claude CLI is spawned; a fake
engine/substrate/loader exercise the option-injection + linkage paths, and the
fork caps are asserted at the handler boundary before any child is built.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from computeragent.harness.coordinator import HarnessCoordinator
from computeragent.harness.fork import (
    FORK_TOOL_QUALIFIED,
    ForkConfig,
    ForkContext,
    append_steer,
    build_fork_mcp_server,
)
from computeragent.protocol_types import AgentManifest, SubstrateHandle
from computeragent.telemetry.event import TelemetryEvent


# ── fakes ──────────────────────────────────────────────────────────────────


async def _noop_close() -> None:
    return None


class _FakeEngine:
    name = "claude-agent-sdk"
    capabilities = None

    async def start_session(self, ctx: Any):  # pragma: no cover - never iterated
        return
        yield  # noqa: unreachable — makes this an async generator


class _FakeSubstrate:
    name = "local"

    async def boot(self, *, source: Any, envs: Any, git_credential: Any = None) -> SubstrateHandle:
        ws = source if isinstance(source, Path) else Path(str(source))
        return SubstrateHandle(workspace_path=ws, close=_noop_close)


class _FakeLoader:
    name = "passthrough"

    def __init__(self, options: dict[str, Any]) -> None:
        self._options = options

    async def load(self, *, source: Any, target_engine: str, workspace_path: Path) -> AgentManifest:
        return AgentManifest(name="parent", version="0.0.0", options=dict(self._options))


class _CapturePipeline:
    def __init__(self) -> None:
        self.events: list[TelemetryEvent] = []

    async def emit(self, ev: TelemetryEvent) -> None:
        self.events.append(ev)

    async def flush(self) -> None:
        return None


def _make_coordinator(
    tmp_path: Path,
    *,
    loader_options: dict[str, Any],
    fork_mode: str = "off",
    fork_depth: int = 0,
    fork_config: ForkConfig | None = None,
    pipeline: _CapturePipeline | None = None,
) -> HarnessCoordinator:
    return HarnessCoordinator(
        engine=_FakeEngine(),
        substrate=_FakeSubstrate(),
        identity_loader=_FakeLoader(loader_options),
        source=tmp_path,
        envs={},
        telemetry_pipeline=pipeline,
        agent_id="parent-agent",
        agent_name="parent",
        fork_mode=fork_mode,
        fork_config=fork_config if fork_config is not None else (ForkConfig() if fork_mode != "off" else None),
        fork_depth=fork_depth,
    )


# ── tool construction ───────────────────────────────────────────────────────


def test_build_fork_server_exposes_spawn_tool():
    ctx = ForkContext(coordinator=object(), config=ForkConfig(), depth=0, mode="manual")  # type: ignore[arg-type]
    server, names = build_fork_mcp_server(ctx)
    assert names == [FORK_TOOL_QUALIFIED] == ["mcp__fork__spawn"]
    assert server is not None


# ── steer shaping ────────────────────────────────────────────────────────────


def test_append_steer_shapes():
    # dict preset keeps the preset and extends append
    d = append_steer({"type": "preset", "preset": "claude_code", "append": "BASE"})
    assert d["preset"] == "claude_code" and d["append"].startswith("BASE")
    assert "mcp__fork__spawn" in d["append"]
    # str concatenates
    s = append_steer("hello")
    assert s.startswith("hello") and "mcp__fork__spawn" in s
    # None → augments the preset (does not replace it)
    n = append_steer(None)
    assert n["type"] == "preset" and n["preset"] == "claude_code"


# ── coordinator option injection ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_off_mode_leaves_options_untouched(tmp_path):
    base = {"allowed_tools": ["Read"], "system_prompt": {"type": "preset", "preset": "claude_code", "append": "X"}}
    coord = _make_coordinator(tmp_path, loader_options=base, fork_mode="off")
    await coord._ensure_booted()
    assert coord._manifest.options == base
    assert "fork" not in (coord._manifest.options.get("mcp_servers") or {})


@pytest.mark.asyncio
async def test_manual_mode_injects_fork_server_and_allowlists_tool(tmp_path):
    base = {"allowed_tools": ["Read", "Bash"]}
    coord = _make_coordinator(tmp_path, loader_options=base, fork_mode="manual")
    await coord._ensure_booted()
    opts = coord._manifest.options
    assert "fork" in opts["mcp_servers"]
    assert FORK_TOOL_QUALIFIED in opts["allowed_tools"]
    # manual mode must NOT disable native sub-agents
    assert "agents" not in opts
    assert "Task" not in (opts.get("disallowed_tools") or [])


@pytest.mark.asyncio
async def test_manual_mode_does_not_introduce_an_allowlist(tmp_path):
    # No allowed_tools set → the fork server is available (all tools allowed),
    # but we must NOT introduce an allowlist (that would restrict to fork only).
    coord = _make_coordinator(tmp_path, loader_options={}, fork_mode="manual")
    await coord._ensure_booted()
    opts = coord._manifest.options
    assert "fork" in opts["mcp_servers"]
    assert "allowed_tools" not in opts


@pytest.mark.asyncio
async def test_auto_mode_disables_native_and_steers(tmp_path):
    base = {"allowed_tools": ["Read"], "agents": {"summarizer": object()}, "system_prompt": "ROLE"}
    coord = _make_coordinator(tmp_path, loader_options=base, fork_mode="auto")
    await coord._ensure_booted()
    opts = coord._manifest.options
    assert opts["agents"] == {}  # native in-process sub-agents disabled
    assert "Task" in opts["disallowed_tools"]
    assert "mcp__fork__spawn" in opts["system_prompt"]  # steer appended (str form)
    assert "fork" in opts["mcp_servers"]


@pytest.mark.asyncio
async def test_child_at_max_depth_gets_no_fork_tool(tmp_path):
    cfg = ForkConfig(max_depth=2)
    coord = _make_coordinator(tmp_path, loader_options={"allowed_tools": ["Read"]}, fork_mode="manual", fork_depth=2, fork_config=cfg)
    await coord._ensure_booted()
    assert "fork" not in (coord._manifest.options.get("mcp_servers") or {})


# ── linkage stamping at the emit chokepoint ──────────────────────────────────


@pytest.mark.asyncio
async def test_emit_stamps_parent_linkage(tmp_path):
    pipe = _CapturePipeline()
    coord = HarnessCoordinator(
        engine=_FakeEngine(),
        substrate=_FakeSubstrate(),
        identity_loader=_FakeLoader({}),
        source=tmp_path,
        envs={},
        telemetry_pipeline=pipe,
        agent_id="child/researcher#abc123",
        agent_name="researcher",
        parent_session_id="ca_parent",
        parent_agent_id="parent-agent",
    )
    await coord._emit(TelemetryEvent(kind="tool_use", session_id="ca_child"))
    [ev] = pipe.events
    assert ev.agent_id == "child/researcher#abc123"
    assert ev.parent_session_id == "ca_parent"
    assert ev.parent_agent_id == "parent-agent"


@pytest.mark.asyncio
async def test_emit_leaves_linkage_none_for_top_level(tmp_path):
    pipe = _CapturePipeline()
    coord = _make_coordinator(tmp_path, loader_options={}, pipeline=pipe)
    await coord._emit(TelemetryEvent(kind="tool_use", session_id="ca_top"))
    [ev] = pipe.events
    assert ev.parent_session_id is None
    assert ev.parent_agent_id is None


# ── fork caps (asserted before any child is built) ───────────────────────────


class _FakeCoord:
    """Just enough surface for a ForkContext to read + not spawn."""

    def __init__(self, tmp_path: Path) -> None:
        self._lifecycle_session_id = "ca_parent"
        self._agent_id = "parent-agent"
        self._agent_name = "parent"
        self._manifest = AgentManifest(name="parent", version="0", options={})
        self._pipeline = _CapturePipeline()
        self._engine = _FakeEngine()
        self._envs = {}
        self._workspace_path = tmp_path


@pytest.mark.asyncio
async def test_spawn_refused_at_max_depth(tmp_path):
    from computeragent.harness.fork import _spawn

    ctx = ForkContext(_FakeCoord(tmp_path), ForkConfig(max_depth=1), depth=1, mode="manual")  # type: ignore[arg-type]
    out = await _spawn(ctx, {"task": "do it", "name": "w", "role": "R"})
    assert out.get("is_error") is True
    assert "max fork depth" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_spawn_refused_at_max_children(tmp_path):
    from computeragent.harness.fork import _spawn

    ctx = ForkContext(_FakeCoord(tmp_path), ForkConfig(max_children=1), depth=0, mode="manual")  # type: ignore[arg-type]
    ctx._count = 1  # already at the lifetime cap
    out = await _spawn(ctx, {"task": "do it", "name": "w", "role": "R"})
    assert out.get("is_error") is True
    assert "max children" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_spawn_requires_task(tmp_path):
    from computeragent.harness.fork import _spawn

    ctx = ForkContext(_FakeCoord(tmp_path), ForkConfig(), depth=0, mode="manual")  # type: ignore[arg-type]
    out = await _spawn(ctx, {"name": "w", "role": "R"})
    assert out.get("is_error") is True
    assert "`task` is required" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_spawn_unknown_preset_errors(tmp_path):
    from computeragent.harness.fork import _spawn

    # No agents/ dir under the parent workspace → preset miss.
    ctx = ForkContext(_FakeCoord(tmp_path), ForkConfig(), depth=0, mode="manual")  # type: ignore[arg-type]
    out = await _spawn(ctx, {"task": "x", "agent": "does-not-exist"})
    assert out.get("is_error") is True
    assert "no sub-agent preset" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_spawn_preset_not_in_allowlist(tmp_path):
    from computeragent.harness.fork import _spawn

    ctx = ForkContext(
        _FakeCoord(tmp_path), ForkConfig(allowed_presets=["only-this"]), depth=0, mode="manual"  # type: ignore[arg-type]
    )
    out = await _spawn(ctx, {"task": "x", "agent": "other"})
    assert out.get("is_error") is True
    assert "not in the allowed set" in out["content"][0]["text"]


# ── serialize carries linkage ────────────────────────────────────────────────


def test_agentos_serialize_includes_parent_fields():
    from computeragent.telemetry.sinks.agentos_http import AgentOSHttpSink

    sink = AgentOSHttpSink(url="http://x/ingest", token="cak_x")
    ev = TelemetryEvent(
        kind="session_started",
        session_id="ca_child",
        agent_id="child#1",
        parent_session_id="ca_parent",
        parent_agent_id="parent-agent",
    )
    dto = sink._serialize(ev)
    assert dto["parent_session_id"] == "ca_parent"
    assert dto["parent_agent_id"] == "parent-agent"
    assert dto["session_id"] == "ca_child"
