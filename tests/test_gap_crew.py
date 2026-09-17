"""GAP ``agents/`` classification: embedded sub-agents vs. referenced workers,
and the orchestration spec the loader surfaces from them."""

from __future__ import annotations

from pathlib import Path

import pytest

from computeragent.identity.gap import GapIdentityLoader
from computeragent.identity.gap_subagents import classify_gap_agents, load_gap_subagents

yaml = pytest.importorskip("yaml")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    write(tmp_path / "agent.yaml", "name: orchestrator\nversion: '1.0.0'\n")
    return tmp_path


# ── classification ─────────────────────────────────────────────────────────


def test_an_inline_prompt_file_is_an_embedded_subagent(repo: Path):
    write(repo / "agents" / "critic.yaml", "description: Critiques\nprompt: Be harsh\n")
    agents = classify_gap_agents(repo)
    assert set(agents.native_subagents) == {"critic"}
    assert agents.workers == ()


def test_a_nested_soul_directory_is_an_embedded_subagent(repo: Path):
    write(repo / "agents" / "scribe" / "SOUL.md", "You write things down.")
    write(repo / "agents" / "scribe" / "agent.yaml", "description: Scribe\n")
    agents = classify_gap_agents(repo)
    assert agents.native_subagents["scribe"].description == "Scribe"
    assert agents.workers == ()


def test_a_use_entry_is_a_referenced_worker_and_never_a_subagent(repo: Path):
    write(
        repo / "agents" / "builder.yaml",
        "use: github.com/org/builder\ndescription: ignored\nprompt: ignored\n",
    )
    agents = classify_gap_agents(repo)
    assert agents.native_subagents == {}
    assert [w.name for w in agents.workers] == ["builder"]
    assert agents.workers[0].use == "github.com/org/builder"


def test_a_referenced_worker_defaults_to_its_own_session(repo: Path):
    write(repo / "agents" / "b.yaml", "use: github.com/org/b\n")
    assert classify_gap_agents(repo).workers[0].separate_session is True


def test_every_referenced_field_is_read(repo: Path):
    write(
        repo / "agents" / "b.yaml",
        yaml.safe_dump(
            {
                "use": "github.com/org/b",
                "separate_session": False,
                "scope": {"write": ["src/**"], "read": ["docs/**"]},
                "budget_usd": 2.5,
                "max_turns": 9,
                "model": "m-1",
                "tools": ["Read", "Write"],
                "mcp": ["jira"],
            }
        ),
    )
    worker = classify_gap_agents(repo).workers[0]
    assert worker.separate_session is False
    assert worker.scope is not None
    assert worker.scope.write == ("src/**",)
    assert worker.scope.read == ("docs/**",)
    assert worker.budget_usd == 2.5
    assert worker.max_turns == 9
    assert worker.model == "m-1"
    assert worker.tool_subset == ("Read", "Write")
    assert worker.mcp == ("jira",)


@pytest.mark.parametrize("value", ["inherit", "none"])
def test_the_mcp_keywords_are_kept_verbatim(repo: Path, value: str):
    write(repo / "agents" / "b.yaml", f"use: github.com/org/b\nmcp: {value}\n")
    assert classify_gap_agents(repo).workers[0].mcp == value


def test_a_nonsense_mcp_value_reads_as_nothing_said(repo: Path):
    # `None` means "fall through to the orchestration block", which resolves to
    # inherit — never a silent "none".
    write(repo / "agents" / "b.yaml", "use: github.com/org/b\nmcp: 17\n")
    assert classify_gap_agents(repo).workers[0].mcp is None


def test_out_of_range_numbers_are_ignored_rather_than_carried(repo: Path):
    write(
        repo / "agents" / "b.yaml",
        "use: github.com/org/b\nmax_turns: 0\nbudget_usd: -3\n",
    )
    worker = classify_gap_agents(repo).workers[0]
    assert worker.max_turns is None
    assert worker.budget_usd is None


def test_a_malformed_sibling_never_breaks_the_rest(repo: Path):
    write(repo / "agents" / "broken.yaml", "use: [unclosed\n")
    write(repo / "agents" / "good.yaml", "use: github.com/org/good\n")
    assert [w.name for w in classify_gap_agents(repo).workers] == ["good"]


def test_a_repo_without_an_agents_directory_classifies_to_nothing(repo: Path):
    agents = classify_gap_agents(repo)
    assert agents.native_subagents == {}
    assert agents.workers == ()


def test_the_native_only_view_is_unchanged_for_a_repo_with_no_use_entries(repo: Path):
    write(repo / "agents" / "critic.yaml", "description: C\nprompt: p\n")
    assert set(load_gap_subagents(repo)) == {"critic"}


def test_the_native_only_view_excludes_referenced_workers(repo: Path):
    write(repo / "agents" / "b.yaml", "use: github.com/org/b\n")
    assert load_gap_subagents(repo) == {}


# ── the spec the loader surfaces ───────────────────────────────────────────


async def test_a_plain_agent_gets_no_orchestration_spec(repo: Path):
    manifest = await GapIdentityLoader().load(
        source=repo, target_engine="claude-agent-sdk", workspace_path=repo
    )
    assert manifest.orchestration is None


async def test_a_use_crew_alone_surfaces_a_spec_with_the_defaults(repo: Path):
    write(repo / "agents" / "b.yaml", "use: github.com/org/b\n")
    manifest = await GapIdentityLoader().load(
        source=repo, target_engine="claude-agent-sdk", workspace_path=repo
    )
    spec = manifest.orchestration
    assert spec is not None
    assert spec.mode == "roster"
    assert spec.max_parallel == 4
    assert spec.collect == "into-context"
    assert spec.isolation == "worktree"
    # A fan-out is one logical run, so splitting the tool surface between the
    # planner and the workers is the surprising default.
    assert spec.worker_mcp == "inherit"
    assert [w.name for w in spec.workers] == ["b"]


async def test_an_orchestration_block_alone_surfaces_a_spec(repo: Path):
    write(
        repo / "agent.yaml",
        yaml.safe_dump(
            {
                "name": "o",
                "version": "1",
                "orchestration": {
                    "mode": "supervised",
                    "max_parallel": 2,
                    "collect": "gate",
                    "isolation": "none",
                    "budget_usd": 6,
                    "worker_mcp": "none",
                },
            }
        ),
    )
    spec = (
        await GapIdentityLoader().load(
            source=repo, target_engine="claude-agent-sdk", workspace_path=repo
        )
    ).orchestration
    assert (spec.mode, spec.max_parallel, spec.collect, spec.isolation) == (
        "supervised",
        2,
        "gate",
        "none",
    )
    assert spec.budget_usd == 6
    assert spec.worker_mcp == "none"
    assert spec.workers == ()


async def test_the_stage_budget_falls_back_to_the_runtime_block(repo: Path):
    write(
        repo / "agent.yaml",
        yaml.safe_dump(
            {
                "name": "o",
                "version": "1",
                "runtime": {"budget_usd": 3.5},
                "orchestration": {"mode": "roster"},
            }
        ),
    )
    spec = (
        await GapIdentityLoader().load(
            source=repo, target_engine="claude-agent-sdk", workspace_path=repo
        )
    ).orchestration
    assert spec.budget_usd == 3.5


async def test_nonsense_block_values_fall_back_to_the_defaults(repo: Path):
    write(
        repo / "agent.yaml",
        yaml.safe_dump(
            {
                "name": "o",
                "version": "1",
                "orchestration": {
                    "mode": "sideways",
                    "max_parallel": -1,
                    "collect": "maybe",
                    "isolation": "cave",
                },
            }
        ),
    )
    spec = (
        await GapIdentityLoader().load(
            source=repo, target_engine="claude-agent-sdk", workspace_path=repo
        )
    ).orchestration
    assert (spec.mode, spec.max_parallel, spec.collect, spec.isolation) == (
        "roster",
        4,
        "into-context",
        "worktree",
    )
