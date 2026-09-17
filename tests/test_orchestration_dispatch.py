"""Spawning workers as sibling sessions: fail-soft, bounded, parent-linked."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from computeragent.orchestration.contract import OrchestrationSpec, WorkerScope
from computeragent.orchestration.spawn_child import (
    OrchestratorRef,
    WorkerSpec,
    dispatch_workers,
    inline_worker_manifest,
    spawn_child_session,
)
from computeragent.orchestration.wave_events import WaveReporter
from computeragent.protocol_types import EngineEvent
from fakes.scripted_engine import FailingEngine, ScriptedEngine, result_message


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "README.md").write_text("hi\n")
    return tmp_path


def parent_ref(workspace: Path, engine, **overrides) -> OrchestratorRef:
    base = {
        "session_id": "ca_parent",
        "engine": engine,
        "engine_name": engine.name,
        "engine_options": {"model": "parent-model"},
        "envs": {"FOO": "bar"},
        "workdir": workspace,
        "agent_id": "agent-1",
        "agent_name": "orchestrator",
        "orchestration": OrchestrationSpec(),
    }
    base.update(overrides)
    return OrchestratorRef(**base)


def spec(workspace: Path, wid="w1", task="do it", **kw) -> WorkerSpec:
    return WorkerSpec(
        id=wid,
        source=workspace,
        task=task,
        inline_manifest=inline_worker_manifest(wid),
        **kw,
    )


# ── one worker ─────────────────────────────────────────────────────────────


async def test_a_worker_runs_and_reports_its_text_and_usage(workspace: Path):
    engine = ScriptedEngine(["I finished the task."])
    result = await spawn_child_session(
        parent_ref(workspace, engine), spec(workspace), workdir=workspace
    )
    assert result.status == "completed"
    assert result.text == "I finished the task."
    assert result.usage.cost_usd == pytest.approx(0.01)
    assert result.error is None


async def test_the_worker_is_handed_its_task_as_the_user_message(workspace: Path):
    engine = ScriptedEngine(["ok"])
    await spawn_child_session(
        parent_ref(workspace, engine),
        spec(workspace, task="build the thing"),
        workdir=workspace,
    )
    assert engine.prompts == ["build the thing"]


async def test_an_engine_failure_comes_back_as_a_result_not_a_raise(workspace: Path):
    # Fail-soft: one worker must never lose the wave.
    result = await spawn_child_session(
        parent_ref(workspace, FailingEngine("boom")), spec(workspace), workdir=workspace
    )
    assert result.status == "errored"
    assert "boom" in result.error


async def test_the_worker_inherits_the_orchestrators_envs(workspace: Path):
    engine = ScriptedEngine(["ok"])
    await spawn_child_session(
        parent_ref(workspace, engine),
        spec(workspace, envs={"EXTRA": "1"}),
        workdir=workspace,
    )
    envs = engine.contexts[0].envs
    assert envs["FOO"] == "bar"
    assert envs["EXTRA"] == "1"


async def test_a_separate_session_worker_gets_its_own_session_id(workspace: Path):
    engine = ScriptedEngine(["ok"])
    result = await spawn_child_session(
        parent_ref(workspace, engine), spec(workspace), workdir=workspace
    )
    assert result.session_id != "ca_parent"


async def test_a_shared_id_worker_folds_under_the_orchestrators_session(workspace: Path):
    engine = ScriptedEngine(["ok"])
    result = await spawn_child_session(
        parent_ref(workspace, engine),
        spec(workspace, session_mode="sharedId"),
        workdir=workspace,
    )
    assert result.session_id == "ca_parent"


async def test_a_scoped_worker_gets_a_scope_guard_that_actually_denies(workspace: Path):
    engine = ScriptedEngine(["ok"])
    await spawn_child_session(
        parent_ref(workspace, engine),
        spec(workspace, scope=WorkerScope(write=("src/**",))),
        workdir=workspace,
    )
    decider = engine.contexts[0].policy_decider
    assert decider is not None
    denied = await decider("Write", {"file_path": "elsewhere.py"}, None)
    assert getattr(denied, "behavior", None) == "deny"


async def test_an_unscoped_worker_still_gets_a_decider_so_it_never_hangs(workspace: Path):
    # An engine that always routes tool calls through the permission path has
    # nobody to answer a worker's request; without a decider its first tool call
    # would block forever and take the wave with it.
    engine = ScriptedEngine(["ok"])
    await spawn_child_session(parent_ref(workspace, engine), spec(workspace), workdir=workspace)
    decider = engine.contexts[0].policy_decider
    allowed = await decider("Write", {"file_path": "anything.py"}, None)
    assert getattr(allowed, "behavior", None) == "allow"


async def test_a_bound_guardrail_cannot_be_escaped_by_fanning_out(workspace: Path):
    calls: list[str] = []

    async def parent_policy(tool_name, tool_input, context):
        from claude_agent_sdk.types import PermissionResultDeny

        calls.append(tool_name)
        return PermissionResultDeny(message="parent policy says no", interrupt=False)

    engine = ScriptedEngine(["ok"])
    await spawn_child_session(
        parent_ref(workspace, engine, policy_decider=parent_policy),
        spec(workspace),
        workdir=workspace,
    )
    decider = engine.contexts[0].policy_decider
    result = await decider("Bash", {}, None)
    assert calls == ["Bash"]
    assert getattr(result, "message", "") == "parent policy says no"


async def test_the_scope_guard_runs_before_the_inherited_guardrail(workspace: Path):
    reached: list[str] = []

    async def parent_policy(tool_name, tool_input, context):
        from claude_agent_sdk.types import PermissionResultAllow

        reached.append(tool_name)
        return PermissionResultAllow(updated_input=tool_input)

    engine = ScriptedEngine(["ok"])
    await spawn_child_session(
        parent_ref(workspace, engine, policy_decider=parent_policy),
        spec(workspace, scope=WorkerScope(write=("src/**",))),
        workdir=workspace,
    )
    decider = engine.contexts[0].policy_decider
    await decider("Write", {"file_path": "elsewhere.py"}, None)
    # The local check is free; an inherited decider may be a network round-trip.
    assert reached == []


async def test_the_worker_inherits_the_orchestrators_mcp_servers(workspace: Path):
    engine = ScriptedEngine(["ok"])
    bridge = {"type": "http", "url": "https://bridge/mcp/jira"}
    await spawn_child_session(
        parent_ref(workspace, engine, engine_options={"mcp_servers": {"jira": bridge}}),
        spec(workspace),
        workdir=workspace,
    )
    assert engine.contexts[0].manifest.options["mcp_servers"] == {"jira": bridge}


async def test_a_worker_can_opt_out_of_the_orchestrators_mcp_servers(workspace: Path):
    engine = ScriptedEngine(["ok"])
    await spawn_child_session(
        parent_ref(
            workspace,
            engine,
            engine_options={"mcp_servers": {"jira": {"type": "http", "url": "u"}}},
        ),
        spec(workspace, mcp="none"),
        workdir=workspace,
    )
    assert "mcp_servers" not in (engine.contexts[0].manifest.options or {})


# ── the pool ───────────────────────────────────────────────────────────────


async def test_results_come_back_in_spec_order_not_completion_order(workspace: Path):
    engine = ScriptedEngine(["ok"])
    specs = [spec(workspace, f"w{i}", f"task {i}") for i in range(5)]
    results = await dispatch_workers(parent_ref(workspace, engine), specs, max_parallel=3)
    assert [r.id for r in results] == ["w0", "w1", "w2", "w3", "w4"]


async def test_concurrency_is_code_controlled_not_model_controlled(workspace: Path):
    live = 0
    peak = 0

    class SlowEngine(ScriptedEngine):
        async def start_session(self, ctx):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            try:
                async for _ in ctx.user_message_queue:
                    break
                await asyncio.sleep(0.01)
                yield EngineEvent(kind="sdk_message", payload=result_message("ok"))
            finally:
                live -= 1

    specs = [spec(workspace, f"w{i}", "t") for i in range(6)]
    await dispatch_workers(parent_ref(workspace, SlowEngine(["ok"])), specs, max_parallel=2)
    assert peak <= 2


async def test_an_empty_spec_list_dispatches_nothing(workspace: Path):
    assert await dispatch_workers(parent_ref(workspace, ScriptedEngine(["ok"])), []) == []


async def test_one_failing_worker_does_not_lose_the_others(workspace: Path):
    class Flaky(ScriptedEngine):
        async def start_session(self, ctx):
            async for entry in ctx.user_message_queue:
                self.prompts.append(str(entry.get("content", "")))
                break
            if self.prompts[-1] == "bad":
                raise RuntimeError("that one broke")
            yield EngineEvent(kind="sdk_message", payload=result_message("fine"))

    specs = [
        spec(workspace, "w1", "good"),
        spec(workspace, "w2", "bad"),
        spec(workspace, "w3", "good"),
    ]
    results = await dispatch_workers(parent_ref(workspace, Flaky(["x"])), specs)
    assert [r.status for r in results] == ["completed", "errored", "completed"]
    assert "that one broke" in results[1].error


async def test_every_worker_reports_a_start_and_a_completion(workspace: Path):
    events: list = []
    reporter = WaveReporter(events.append, "ca_parent", "wave_1")
    specs = [spec(workspace, "w1", "t"), spec(workspace, "w2", "t")]
    await dispatch_workers(parent_ref(workspace, ScriptedEngine(["ok"])), specs, reporter=reporter)
    kinds = [e.kind for e in events]
    assert kinds.count("worker_started") == 2
    assert kinds.count("worker_completed") == 2


async def test_a_worker_that_failed_to_construct_still_reports_a_completion(
    workspace: Path,
):
    # Otherwise its row spins forever on a caller's screen.
    events: list = []
    reporter = WaveReporter(events.append, "ca_parent", "wave_1")
    bad = WorkerSpec(id="w1", source=workspace, task="t", engine="no-such-engine")
    results = await dispatch_workers(
        parent_ref(workspace, ScriptedEngine(["ok"])), [bad], reporter=reporter
    )
    assert results[0].status == "errored"
    assert [e.kind for e in events] == ["worker_completed"]


async def test_a_completion_carries_the_wave_id_and_duration(workspace: Path):
    events: list = []
    reporter = WaveReporter(events.append, "ca_parent", "wave_7")
    await dispatch_workers(
        parent_ref(workspace, ScriptedEngine(["ok"])), [spec(workspace)], reporter=reporter
    )
    done = next(e for e in events if e.kind == "worker_completed")
    assert done.payload["wave_id"] == "wave_7"
    assert done.payload["duration_ms"] >= 0
    assert done.payload["usage"]["output_tokens"] == 5


async def test_a_reporter_whose_consumer_went_away_never_aborts_the_wave(
    workspace: Path,
):
    def explode(_ev):
        raise RuntimeError("stream closed")

    reporter = WaveReporter(explode, "ca_parent", "wave_1")
    results = await dispatch_workers(
        parent_ref(workspace, ScriptedEngine(["ok"])), [spec(workspace)], reporter=reporter
    )
    assert results[0].status == "completed"


async def test_a_worker_never_fans_out_even_when_its_own_repo_says_to(workspace: Path):
    # A crew repo reused as a worker is the obvious case, and nothing would
    # bound the recursion: each level clones, opens worktrees and spends the
    # caller's budget again. The TS harness gets this structurally — its
    # fan-out branch lives in `driveSession` and a worker is started with
    # `runSession` — so here it has to be explicit.
    from computeragent.protocol_types import AgentManifest

    engine = ScriptedEngine(['```json\n{"workers": [{"id": "w1", "task": "recurse"}]}\n```'])
    nested = WorkerSpec(
        id="w1",
        source=workspace,
        task="go",
        inline_manifest=AgentManifest(
            name="w1",
            version="0",
            options={},
            orchestration=OrchestrationSpec(mode="roster", isolation="worktree"),
        ),
    )
    result = await spawn_child_session(parent_ref(workspace, engine), nested, workdir=workspace)
    assert result.status == "completed"
    # One turn only: no closing synthesis, so no wave ran inside the worker.
    assert len(engine.prompts) == 1
