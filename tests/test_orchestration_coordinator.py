"""The harness drive: a roster orchestrator's turn, its fan-out, and the closing
turn that writes the answer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from computeragent.harness.coordinator import HarnessCoordinator
from computeragent.orchestration.contract import OrchestrationSpec
from computeragent.protocol_types import AgentManifest
from computeragent.substrates import LocalSubstrate
from fakes.scripted_engine import ScriptedEngine


def roster(*workers: dict) -> str:
    return "Plan:\n\n```json\n" + json.dumps({"workers": list(workers)}) + "\n```"


class OrchestratorLoader:
    """Returns a manifest carrying an orchestration spec, without a GAP repo."""

    name = "test-orchestrator"

    def __init__(self, spec: OrchestrationSpec | None, options: dict | None = None) -> None:
        self.spec = spec
        self.options = options or {}

    async def load(self, *, source, target_engine, workspace_path):
        return AgentManifest(
            name="orchestrator",
            version="1",
            options=dict(self.options),
            orchestration=self.spec,
        )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "agent.yaml").write_text("name: orchestrator\nversion: '1'\n")
    (repo / "README.md").write_text("baseline\n")
    return repo


def build(workspace: Path, engine, spec, **kw) -> HarnessCoordinator:
    return HarnessCoordinator(
        engine=engine,
        substrate=LocalSubstrate(),
        identity_loader=OrchestratorLoader(spec, kw.pop("options", None)),
        source=workspace,
        envs={},
        agent_id="agent-1",
        agent_name="orchestrator",
        **kw,
    )


async def drain(coordinator: HarnessCoordinator, message: str) -> list:
    _state, stream = await coordinator.run_chat(message)
    return [ev async for ev in stream]


def kinds(events: list) -> list[str]:
    return [e.kind for e in events]


# ── a plain agent is untouched ─────────────────────────────────────────────


async def test_an_agent_with_no_orchestration_runs_exactly_one_turn(workspace: Path):
    engine = ScriptedEngine(["just an answer"])
    coordinator = build(workspace, engine, None)
    events = await drain(coordinator, "hello")
    assert engine.prompts == ["hello"]
    assert "worker_wave_started" not in kinds(events)
    await coordinator.dispose()


async def test_a_plain_agent_never_gets_a_dispatch_hook(workspace: Path):
    engine = ScriptedEngine(["answer"])
    coordinator = build(workspace, engine, None)
    await drain(coordinator, "hello")
    assert engine.contexts[0].dispatch is None
    await coordinator.dispose()


# ── the roster contract ────────────────────────────────────────────────────


async def test_a_roster_orchestrator_gets_the_contract_appended(workspace: Path):
    engine = ScriptedEngine(["prose answer"])
    coordinator = build(
        workspace,
        engine,
        OrchestrationSpec(mode="roster"),
        options={"system_prompt": {"type": "preset", "preset": "claude_code", "append": "# Soul"}},
    )
    await drain(coordinator, "hi")
    append = engine.contexts[0].manifest.options["system_prompt"]["append"]
    assert append.startswith("# Soul")
    assert "NEVER emit a roster with an empty worker list" in append
    await coordinator.dispose()


async def test_a_supervised_orchestrator_does_not_get_the_roster_contract(
    workspace: Path,
):
    engine = ScriptedEngine(["answer"])
    coordinator = build(workspace, engine, OrchestrationSpec(mode="supervised"))
    await drain(coordinator, "hi")
    options = engine.contexts[0].manifest.options
    assert "roster JSON block" not in json.dumps(options.get("system_prompt"), default=str)
    await coordinator.dispose()


# ── prose stands on its own ────────────────────────────────────────────────


async def test_a_roster_agent_that_answers_in_prose_keeps_its_answer(workspace: Path):
    # Synthesising here would spend a call AND overwrite a good answer with a
    # summary of nothing.
    engine = ScriptedEngine(["The answer is 42."])
    coordinator = build(workspace, engine, OrchestrationSpec(mode="roster"))
    events = await drain(coordinator, "what is the answer?")
    assert engine.prompts == ["what is the answer?"]
    await coordinator.dispose()
    assert coordinator._lifecycle_last_assistant_text == "The answer is 42."
    # Matching the TS port: only an EMPTY ROSTER withholds its wave events. A
    # prose answer still reports an empty wave — it is a deviation from the
    # contract the agent's prompt states, and a `collect: "gate"` run is meant
    # to fail on it.
    ended = next(e for e in events if e.kind == "worker_wave_ended")
    assert ended.payload["dispatched"] == 0


# ── an empty roster takes a direct-answer turn, silently ───────────────────


async def test_an_empty_roster_is_never_shown_and_costs_one_closing_turn(
    workspace: Path,
):
    engine = ScriptedEngine(['```json\n{"workers": []}\n```', "The answer is 42."])
    coordinator = build(workspace, engine, OrchestrationSpec(mode="roster"))
    events = await drain(coordinator, "what is the answer?")

    assert len(engine.prompts) == 2
    # The direct-answer prompt, NOT a wave digest: asking the model to summarise
    # a wave that ran nobody produces a summary of nothing or a flat refusal.
    assert "EMPTY roster" in engine.prompts[1]
    assert "Wave summary" not in engine.prompts[1]
    # No wave events: nothing was planned, so there is no wave to report.
    assert "worker_wave_started" not in kinds(events)
    await coordinator.dispose()
    assert coordinator._lifecycle_last_assistant_text == "The answer is 42."


async def test_an_unparseable_roster_takes_the_other_direct_answer_flavour(
    workspace: Path,
):
    engine = ScriptedEngine(['```json\n{"workers": [{"task": "un"escaped"}]}\n```', "answer"])
    coordinator = build(workspace, engine, OrchestrationSpec(mode="roster"))
    await drain(coordinator, "q")
    assert "could not dispatch from" in engine.prompts[1]
    await coordinator.dispose()


async def test_the_synthesis_kill_switch_stops_the_closing_turn(workspace: Path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_SYNTHESIS", "0")
    engine = ScriptedEngine(['```json\n{"workers": []}\n```', "unused"])
    coordinator = build(workspace, engine, OrchestrationSpec(mode="roster"))
    await drain(coordinator, "q")
    assert len(engine.prompts) == 1
    await coordinator.dispose()


# ── a real fan-out ─────────────────────────────────────────────────────────


class RosterThenAnswer(ScriptedEngine):
    """Composes a roster on turn 1, answers on turn 2, and writes a file when
    it is a worker (its prompt is the worker's task)."""

    def __init__(self, roster_text: str, answer: str, files: dict[str, str]) -> None:
        super().__init__([roster_text, answer])
        self.files = files

    async def start_session(self, ctx):
        async for entry in ctx.user_message_queue:
            self.prompts.append(str(entry.get("content", "")))
            break
        self.contexts.append(ctx)
        prompt = self.prompts[-1]
        if prompt in self.files:
            target = Path(ctx.workspace_path) / self.files[prompt]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"by {prompt}\n")
            text = f"finished {prompt}"
        else:
            text = self.replies[min(len(self.replies) - 1, self._orchestrator_turn())]
        from computeragent.protocol_types import EngineEvent
        from fakes.scripted_engine import assistant_message, result_message

        yield EngineEvent(kind="sdk_message", payload=assistant_message(text))
        yield EngineEvent(kind="sdk_message", payload=result_message(text))

    def _orchestrator_turn(self) -> int:
        return sum(1 for p in self.prompts if p not in self.files) - 1


async def test_a_roster_fans_out_merges_and_then_answers(workspace: Path):
    engine = RosterThenAnswer(
        roster(
            {"id": "w1", "task": "build the api", "scope": {"write": ["src/**"]}},
            {"id": "w2", "task": "write the docs", "scope": {"write": ["docs/**"]}},
        ),
        "Both workers finished; the API and docs are in place.",
        {"build the api": "src/api.py", "write the docs": "docs/api.md"},
    )
    coordinator = build(workspace, engine, OrchestrationSpec(mode="roster"))
    events = await drain(coordinator, "build me an api")

    seen = kinds(events)
    assert seen.count("worker_wave_started") == 1
    assert seen.count("worker_started") == 2
    assert seen.count("worker_completed") == 2
    assert seen.count("worker_wave_ended") == 1

    ended = next(e for e in events if e.kind == "worker_wave_ended")
    assert set(ended.payload["integrated"]) == {"src/api.py", "docs/api.md"}
    assert ended.payload["conflicts"] == {}

    # The closing turn was handed the workers' reports…
    digest = engine.prompts[-1]
    assert "2 dispatched, 2 completed, 0 did not" in digest
    assert "files written: src/api.py" in digest
    assert "Do NOT emit another roster" in digest

    await coordinator.dispose()
    # …and what the run REPORTS is that turn's answer, not the roster.
    assert coordinator._lifecycle_last_assistant_text.startswith("Both workers finished")


async def test_the_wave_id_advances_across_two_chats(workspace: Path):
    # Worker ids repeat across waves (`w1` again), so the wave id is what keeps
    # the two waves' rows apart.
    engine = RosterThenAnswer(roster({"id": "w1", "task": "do it"}), "done", {"do it": "src/a.py"})
    coordinator = build(workspace, engine, OrchestrationSpec(mode="roster"))
    first = await drain(coordinator, "one")
    engine.prompts.clear()
    second = await drain(coordinator, "two")
    assert first[0].payload is not None
    wave_ids = [e.payload["wave_id"] for e in (*first, *second) if e.kind == "worker_wave_started"]
    assert wave_ids == ["wave_1", "wave_2"]
    await coordinator.dispose()


async def test_the_closing_turn_resumes_the_orchestrators_engine_session(
    workspace: Path,
):
    engine = RosterThenAnswer(roster({"id": "w1", "task": "do it"}), "done", {"do it": "src/a.py"})
    coordinator = build(workspace, engine, OrchestrationSpec(mode="roster"))
    await drain(coordinator, "go")
    orchestrator_turns = [c for c in engine.contexts if c.session_id.startswith("ca_")]
    # Turn 2 must continue the conversation, or the model answers with no idea
    # what it was asked.
    assert orchestrator_turns[-1].resume_session_id == "eng_1"
    await coordinator.dispose()


async def test_a_gate_run_emits_its_verdict_instead_of_only_logging_it(
    workspace: Path,
):
    engine = RosterThenAnswer(roster({"id": "w1", "task": "do it"}), "done", {"do it": "src/a.py"})
    coordinator = build(workspace, engine, OrchestrationSpec(mode="roster", collect="gate"))
    events = await drain(coordinator, "go")
    card = next(e for e in events if e.kind == "orchestration_scorecard")
    assert card.payload["verdict"] == "pass"
    assert card.payload["scorecard"]["workers"][0]["id"] == "w1"
    assert "Gate verdict: pass" in engine.prompts[-1]
    await coordinator.dispose()


async def test_an_empty_roster_is_not_scored_at_all(workspace: Path):
    # Scoring it produced the worst possible verdict for a turn where nothing
    # was wrong.
    engine = ScriptedEngine(['```json\n{"workers": []}\n```', "answer"])
    coordinator = build(workspace, engine, OrchestrationSpec(mode="roster", collect="gate"))
    events = await drain(coordinator, "q")
    assert "orchestration_scorecard" not in kinds(events)
    await coordinator.dispose()


async def test_wave_events_reach_the_telemetry_pipeline(workspace: Path, stub_sink):
    from computeragent.telemetry import configure

    pipeline = configure(sinks=[stub_sink])
    engine = RosterThenAnswer(roster({"id": "w1", "task": "do it"}), "done", {"do it": "src/a.py"})
    coordinator = build(
        workspace, engine, OrchestrationSpec(mode="roster"), telemetry_pipeline=pipeline
    )
    await drain(coordinator, "go")
    await coordinator.dispose()
    seen = {e.kind for e in stub_sink.events}
    assert {"worker_wave_started", "worker_started", "worker_completed"} <= seen


async def test_a_fan_out_that_explodes_keeps_the_agents_own_text(workspace: Path, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("the wave broke")

    monkeypatch.setattr(
        "computeragent.orchestration.worktree_dispatch.dispatch_worktree_roster", boom
    )
    engine = ScriptedEngine([roster({"id": "w1", "task": "t"}), "unused"])
    coordinator = build(workspace, engine, OrchestrationSpec(mode="roster"))
    await drain(coordinator, "go")
    assert len(engine.prompts) == 1
    await coordinator.dispose()


# ── the registry's own orchestration block ─────────────────────────────────


async def test_registry_overrides_layer_over_the_loaders_spec(workspace: Path):
    # The repo is the only place that can name a crew; the registry is where an
    # operator turns the knobs without a commit. Neither replaces the other.
    engine = ScriptedEngine(["prose"])
    coordinator = build(
        workspace,
        engine,
        OrchestrationSpec(mode="roster", max_parallel=4, collect="into-context"),
        orchestration_overrides={"maxParallel": 2, "collect": "gate"},
    )
    await drain(coordinator, "hi")
    spec = coordinator._manifest.orchestration
    assert spec.max_parallel == 2
    assert spec.collect == "gate"
    # Untouched fields keep what the repo declared.
    assert spec.mode == "roster"
    await coordinator.dispose()


async def test_a_registry_block_creates_a_spec_for_a_repo_that_declares_none(
    workspace: Path,
):
    # A worktree fan-out needs no crew — its workers are defined inline by the
    # roster the model writes.
    engine = ScriptedEngine(["prose"])
    coordinator = build(workspace, engine, None, orchestration_overrides={"mode": "roster"})
    await drain(coordinator, "hi")
    assert coordinator._manifest.orchestration.mode == "roster"
    await coordinator.dispose()


async def test_no_overrides_leaves_the_loaders_spec_exactly_as_it_was(workspace: Path):
    engine = ScriptedEngine(["prose"])
    spec = OrchestrationSpec(mode="roster", max_parallel=9)
    coordinator = build(workspace, engine, spec, orchestration_overrides=None)
    await drain(coordinator, "hi")
    assert coordinator._manifest.orchestration.max_parallel == 9
    await coordinator.dispose()
