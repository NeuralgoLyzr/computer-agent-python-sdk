"""The two dispatch surfaces end to end: independent clones (crew-matched) and
the shared-codebase worktree wave."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from computeragent.orchestration.contract import (
    OrchestrationSpec,
    WorkerCrewEntry,
)
from computeragent.orchestration.parse_roster import parse_roster
from computeragent.orchestration.roster_run import dispatch_roster, dispatch_roster_entries
from computeragent.orchestration.spawn_child import OrchestratorRef
from computeragent.orchestration.wave_events import WaveReporter
from computeragent.orchestration.worktree_dispatch import dispatch_worktree_roster
from fakes.scripted_engine import ScriptedEngine


def roster(*workers: dict) -> str:
    return "Plan:\n\n```json\n" + json.dumps({"workers": list(workers)}) + "\n```"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "agent.yaml").write_text("name: orchestrator\nversion: '1'\n")
    (repo / "README.md").write_text("baseline\n")
    return repo


@pytest.fixture
def worker_repo(tmp_path: Path) -> Path:
    """A referenced worker's own checkout.

    A local path, not a git URL: `use_to_source` resolves `/abs` to a Path, and
    `LocalSubstrate` boots one as a pass-through. Pointing the crew at a real
    remote would make every assertion below one about clone failures.
    """
    repo = tmp_path / "worker-repo"
    repo.mkdir()
    (repo / "README.md").write_text("a worker's own repo\n")
    return repo


def parent(workspace: Path, engine, spec: OrchestrationSpec) -> OrchestratorRef:
    return OrchestratorRef(
        session_id="ca_parent",
        engine=engine,
        engine_name=engine.name,
        engine_options={"model": "parent-model"},
        envs={},
        workdir=workspace,
        agent_id="agent-1",
        agent_name="orchestrator",
        orchestration=spec,
    )


def kinds(events: list) -> list[str]:
    return [e.kind for e in events]


# ── independent mode (crew-matched clones) ─────────────────────────────────


def crew(worker_repo: Path, **kw) -> tuple[WorkerCrewEntry, ...]:
    return (
        WorkerCrewEntry(name="builder", use=str(worker_repo), **kw),
        WorkerCrewEntry(name="reviewer", use=str(worker_repo), **kw),
    )


def clone_spec(worker_repo: Path, **kw) -> OrchestrationSpec:
    return OrchestrationSpec(isolation="none", workers=crew(worker_repo), **kw)


async def test_roster_entries_are_matched_to_crew_members(workspace: Path, worker_repo: Path):
    spec = clone_spec(worker_repo)
    engine = ScriptedEngine(["worker done"])
    result = await dispatch_roster(
        parent(workspace, engine, spec),
        spec,
        roster({"use": "builder", "task": "build"}, {"use": "reviewer", "task": "review"}),
    )
    assert result.dispatched == 2
    assert [w.id for w in result.workers] == ["builder", "reviewer"]
    # Status, not just ids: a crew `use:` that cannot be booted comes back as a
    # wave of errored workers, which would satisfy an ids-only assertion.
    assert [w.status for w in result.workers] == ["completed", "completed"]
    assert [w.text for w in result.workers] == ["worker done", "worker done"]
    assert result.unmatched == ()
    assert engine.prompts == ["build", "review"]


async def test_a_roster_id_overrides_the_crew_name_as_the_instance_id(
    workspace: Path, worker_repo: Path
):
    spec = clone_spec(worker_repo)
    engine = ScriptedEngine(["done"])
    result = await dispatch_roster(
        parent(workspace, engine, spec),
        spec,
        roster({"use": "builder", "id": "b1", "task": "t"}),
    )
    assert [w.id for w in result.workers] == ["b1"]


async def test_an_entry_matching_no_crew_member_is_reported_not_silently_dropped(
    workspace: Path, worker_repo: Path
):
    spec = clone_spec(worker_repo)
    engine = ScriptedEngine(["done"])
    result = await dispatch_roster(
        parent(workspace, engine, spec),
        spec,
        roster({"use": "ghost", "task": "t"}),
    )
    assert result.dispatched == 0
    assert result.unmatched == ("ghost",)


async def test_a_single_worker_crew_is_unambiguous_without_a_use_key(
    workspace: Path, worker_repo: Path
):
    spec = OrchestrationSpec(isolation="none", workers=(crew(worker_repo)[0],))
    engine = ScriptedEngine(["done"])
    result = await dispatch_roster(parent(workspace, engine, spec), spec, roster({"task": "t"}))
    assert [w.id for w in result.workers] == ["builder"]


async def test_an_empty_roster_dispatches_nobody_and_stays_silent(
    workspace: Path, worker_repo: Path
):
    spec = clone_spec(worker_repo)
    events: list = []
    engine = ScriptedEngine(["done"])
    result = await dispatch_roster(
        parent(workspace, engine, spec),
        spec,
        '```json\n{"workers": []}\n```',
        WaveReporter(events.append, "ca_parent", "wave_1"),
    )
    # Announcing a started/ended pair here renders a "sub-agents dispatched"
    # panel over a run that dispatched nobody.
    assert result.outcome == "empty-roster"
    assert events == []


async def test_a_roster_that_matched_nobody_keeps_its_wave_events(
    workspace: Path, worker_repo: Path
):
    spec = clone_spec(worker_repo)
    # That is a real config error and the gate must fail on it, so the loudness
    # is the point.
    events: list = []
    engine = ScriptedEngine(["done"])
    await dispatch_roster(
        parent(workspace, engine, spec),
        spec,
        roster({"use": "ghost", "task": "t"}),
        WaveReporter(events.append, "ca_parent", "wave_1"),
    )
    assert kinds(events) == ["worker_wave_started", "worker_wave_ended"]
    assert events[-1].payload["unmatched"] == ["ghost"]


async def test_the_wave_is_announced_with_the_ids_dispatch_will_actually_use(
    workspace: Path, worker_repo: Path
):
    spec = clone_spec(worker_repo)
    # Announcing from the parsed roster would name ids that dispatch renames,
    # and a caller keying its rows on them would never match a later event.
    events: list = []
    engine = ScriptedEngine(["done"])
    await dispatch_roster(
        parent(workspace, engine, spec),
        spec,
        roster({"use": "builder", "task": "t"}),
        WaveReporter(events.append, "ca_parent", "wave_1"),
    )
    planned = events[0].payload["planned"]
    assert [p["id"] for p in planned] == ["builder"]
    started = next(e for e in events if e.kind == "worker_started")
    assert started.payload["worker_id"] == "builder"


async def test_an_underfunded_wave_is_refused_before_any_worker_runs(
    workspace: Path, worker_repo: Path
):
    # $0.05 across two workers is $0.025 each — under the viable minimum.
    spec = clone_spec(worker_repo, budget_usd=0.05)
    engine = ScriptedEngine(["done"])
    result = await dispatch_roster_entries(
        parent(workspace, engine, spec),
        spec,
        parse_roster(roster({"use": "builder", "task": "t"}, {"use": "reviewer", "task": "t"})),
    )
    assert result.dispatched == 0
    assert result.workers == ()
    assert "Aborting before dispatch" in result.aborted
    assert engine.prompts == []


async def test_a_tighter_crew_cap_still_wins_over_the_fair_share(
    workspace: Path, worker_repo: Path
):
    entry = WorkerCrewEntry(name="b", use=str(worker_repo), budget_usd=0.25)
    spec = OrchestrationSpec(isolation="none", budget_usd=10.0, workers=(entry,))
    engine = ScriptedEngine(["done"])
    await dispatch_roster(parent(workspace, engine, spec), spec, roster({"use": "b", "task": "t"}))
    assert engine.contexts[0].manifest.options["max_budget_usd"] == pytest.approx(0.25)


async def test_the_wave_budget_is_settled_from_what_the_workers_actually_cost(
    workspace: Path, worker_repo: Path
):
    spec = clone_spec(worker_repo, budget_usd=10.0)
    engine = ScriptedEngine(["done"], cost=0.25)
    result = await dispatch_roster(
        parent(workspace, engine, spec),
        spec,
        roster({"use": "builder", "task": "t"}, {"use": "reviewer", "task": "t"}),
    )
    assert result.budget is not None
    assert result.budget.spent_usd == pytest.approx(0.5)
    assert result.budget.estimated_share == 0.0


# ── shared-codebase (worktree) mode ────────────────────────────────────────


WORKTREE_SPEC = OrchestrationSpec(isolation="worktree")


class WritingEngine(ScriptedEngine):
    """Writes one file into its own cwd, so the diff has something to attribute."""

    def __init__(self, filename_by_task: dict[str, str]) -> None:
        super().__init__(["wrote it"])
        self.filename_by_task = filename_by_task

    async def start_session(self, ctx):
        async for entry in ctx.user_message_queue:
            self.prompts.append(str(entry.get("content", "")))
            break
        self.contexts.append(ctx)
        name = self.filename_by_task.get(self.prompts[-1])
        if name:
            target = Path(ctx.workspace_path) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"written for {self.prompts[-1]}\n")
        from computeragent.protocol_types import EngineEvent
        from fakes.scripted_engine import result_message

        yield EngineEvent(kind="sdk_message", payload=result_message("wrote it"))


async def test_each_worker_edits_its_own_worktree_and_the_branches_merge(
    workspace: Path,
):
    engine = WritingEngine({"build the api": "src/api.py", "write the docs": "docs/api.md"})
    result = await dispatch_worktree_roster(
        parent(workspace, engine, WORKTREE_SPEC),
        WORKTREE_SPEC,
        roster(
            {"id": "w1", "task": "build the api", "scope": {"write": ["src/**"]}},
            {"id": "w2", "task": "write the docs", "scope": {"write": ["docs/**"]}},
        ),
    )
    assert result.dispatched == 2
    assert {w.id for w in result.workers} == {"w1", "w2"}
    assert set(result.integrated) == {"src/api.py", "docs/api.md"}
    assert result.conflicts == {}


async def test_artifacts_are_attributed_per_worker_before_the_merge(workspace: Path):
    engine = WritingEngine({"a": "src/a.py", "b": "docs/b.md"})
    result = await dispatch_worktree_roster(
        parent(workspace, engine, WORKTREE_SPEC),
        WORKTREE_SPEC,
        roster({"id": "w1", "task": "a"}, {"id": "w2", "task": "b"}),
    )
    by_id = {w.id: w for w in result.workers}
    assert by_id["w1"].artifacts == ("src/a.py",)
    assert by_id["w2"].artifacts == ("docs/b.md",)


async def test_two_workers_writing_the_same_file_surface_as_a_conflict(
    workspace: Path,
):
    engine = WritingEngine({"a": "README.md", "b": "README.md"})
    result = await dispatch_worktree_roster(
        parent(workspace, engine, WORKTREE_SPEC),
        WORKTREE_SPEC,
        roster({"id": "w1", "task": "a"}, {"id": "w2", "task": "b"}),
    )
    assert "w2" in result.conflicts
    assert result.conflicts["w2"] == ("README.md",)


async def test_the_orchestrators_own_manifest_survives_a_multi_worker_wave(
    workspace: Path,
):
    # Every worker's branch modifying `agent.yaml` is what used to make any wave
    # of ≥2 workers conflict on a file none of them touched — and `merge
    # --abort` is atomic, so the second worker's whole diff was discarded.
    before = (workspace / "agent.yaml").read_text()
    engine = WritingEngine({"a": "src/a.py", "b": "src/b.py"})
    result = await dispatch_worktree_roster(
        parent(workspace, engine, WORKTREE_SPEC),
        WORKTREE_SPEC,
        roster({"id": "w1", "task": "a"}, {"id": "w2", "task": "b"}),
    )
    assert result.conflicts == {}
    assert "agent.yaml" not in result.integrated
    assert (workspace / "agent.yaml").read_text() == before


async def test_a_worker_gets_its_role_and_scope_in_its_prompt(workspace: Path):
    engine = ScriptedEngine(["done"])
    await dispatch_worktree_roster(
        parent(workspace, engine, WORKTREE_SPEC),
        WORKTREE_SPEC,
        roster({"id": "w1", "role": "api builder", "task": "t", "scope": {"write": ["src/**"]}}),
    )
    append = engine.contexts[0].manifest.options["system_prompt"]["append"]
    assert 'You are worker "w1" (role: api builder).' in append
    assert "Write ONLY within: src/**." in append


async def test_a_worker_inherits_the_orchestrators_model_when_the_roster_names_none(
    workspace: Path,
):
    # Without the fallback a worker sent no model at all and the CLI substituted
    # its own default, which a gateway-routed deployment rejects outright.
    engine = ScriptedEngine(["done"])
    await dispatch_worktree_roster(
        parent(workspace, engine, WORKTREE_SPEC),
        WORKTREE_SPEC,
        roster({"id": "w1", "task": "t"}),
    )
    assert engine.contexts[0].manifest.options["model"] == "parent-model"


async def test_a_roster_named_model_wins_over_the_inherited_one(workspace: Path):
    engine = ScriptedEngine(["done"])
    await dispatch_worktree_roster(
        parent(workspace, engine, WORKTREE_SPEC),
        WORKTREE_SPEC,
        roster({"id": "w1", "task": "t", "model": "roster-model"}),
    )
    assert engine.contexts[0].manifest.options["model"] == "roster-model"


async def test_an_empty_worktree_roster_dispatches_nothing_and_stays_silent(
    workspace: Path,
):
    events: list = []
    engine = ScriptedEngine(["done"])
    result = await dispatch_worktree_roster(
        parent(workspace, engine, WORKTREE_SPEC),
        WORKTREE_SPEC,
        '```json\n{"workers": []}\n```',
        WaveReporter(events.append, "ca_parent", "wave_1"),
    )
    assert result.dispatched == 0
    assert events == []


async def test_an_underfunded_worktree_wave_aborts_before_touching_git(
    workspace: Path,
):
    spec = OrchestrationSpec(isolation="worktree", budget_usd=0.01)
    engine = ScriptedEngine(["done"])
    result = await dispatch_worktree_roster(
        parent(workspace, engine, spec), spec, roster({"id": "w1", "task": "t"})
    )
    assert result.aborted is not None
    assert not (workspace / ".git").exists()


async def test_the_worktrees_are_cleaned_up_after_the_wave(workspace: Path):
    engine = WritingEngine({"a": "src/a.py"})
    await dispatch_worktree_roster(
        parent(workspace, engine, WORKTREE_SPEC),
        WORKTREE_SPEC,
        roster({"id": "w1", "task": "a"}),
    )
    assert not (workspace.parent / "repo.worktrees").exists()


async def test_a_wave_reports_started_and_completed_for_every_worker(workspace: Path):
    events: list = []
    engine = WritingEngine({"a": "src/a.py", "b": "src/b.py"})
    await dispatch_worktree_roster(
        parent(workspace, engine, WORKTREE_SPEC),
        WORKTREE_SPEC,
        roster({"id": "w1", "task": "a"}, {"id": "w2", "task": "b"}),
        WaveReporter(events.append, "ca_parent", "wave_1"),
    )
    counted = kinds(events)
    assert counted[0] == "worker_wave_started"
    assert counted[-1] == "worker_wave_ended"
    assert counted.count("worker_started") == 2
    assert counted.count("worker_completed") == 2
    assert set(events[-1].payload["integrated"]) == {"src/a.py", "src/b.py"}
