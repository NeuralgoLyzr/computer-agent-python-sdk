"""Crew entries: `use:` resolution and the static-config → runnable-spec map."""

from __future__ import annotations

from pathlib import Path

import pytest

from computeragent.orchestration.contract import WorkerCrewEntry, WorkerScope
from computeragent.orchestration.crew import crew_entry_to_worker_spec, use_to_source


@pytest.mark.parametrize(
    ("use", "expected"),
    [
        ("https://github.com/org/repo", "https://github.com/org/repo"),
        ("http://host/org/repo", "http://host/org/repo"),
        ("ssh://git@host/org/repo", "ssh://git@host/org/repo"),
        ("git@github.com:org/repo.git", "git@github.com:org/repo.git"),
        # The GAP convention: a bare host/org/repo gains https://.
        ("github.com/org/repo", "https://github.com/org/repo"),
        # A bare registered-agent id is not resolvable here; treating it as a
        # git url is the documented best effort, and it fails at clone.
        ("some-agent-id", "https://some-agent-id"),
    ],
)
def test_git_forms_resolve_to_a_url(use, expected):
    assert use_to_source(use) == expected


@pytest.mark.parametrize("prefix", ["/", "./", "../", "~/"])
def test_path_forms_resolve_to_a_path(prefix):
    source = use_to_source(f"{prefix}some/repo")
    assert isinstance(source, Path)


def test_an_at_ref_is_split_off_the_url():
    assert use_to_source("github.com/org/repo@v2") == "https://github.com/org/repo"


def test_an_scp_style_prefix_is_not_mistaken_for_a_ref():
    # The `@` in `git@host` precedes the first `/`, so it is part of the host.
    assert use_to_source("git@github.com:org/repo.git") == "git@github.com:org/repo.git"


def test_surrounding_whitespace_is_ignored():
    assert use_to_source("  github.com/org/repo  ") == "https://github.com/org/repo"


# ── crew entry → worker spec ───────────────────────────────────────────────


def test_static_config_lands_on_the_options_bag():
    entry = WorkerCrewEntry(
        name="reviewer",
        use="github.com/org/reviewer",
        model="m-1",
        tool_subset=("Read", "Grep"),
        max_turns=7,
        budget_usd=2.5,
    )
    spec = crew_entry_to_worker_spec(entry, "review the diff")
    assert spec.id == "reviewer"
    assert spec.task == "review the diff"
    assert spec.source == "https://github.com/org/reviewer"
    assert spec.options == {
        "model": "m-1",
        "allowed_tools": ["Read", "Grep"],
        "max_turns": 7,
        "max_budget_usd": 2.5,
    }


def test_a_bare_entry_carries_no_options_at_all():
    spec = crew_entry_to_worker_spec(WorkerCrewEntry(name="w", use="github.com/o/r"), "t")
    assert spec.options is None


def test_separate_session_maps_to_the_session_mode():
    assert (
        crew_entry_to_worker_spec(
            WorkerCrewEntry(name="w", use="u", separate_session=True), "t"
        ).session_mode
        == "separate"
    )
    assert (
        crew_entry_to_worker_spec(
            WorkerCrewEntry(name="w", use="u", separate_session=False), "t"
        ).session_mode
        == "sharedId"
    )


def test_mcp_stays_off_the_options_bag():
    # `options` is engine config; `mcp` is a policy the spawn site resolves
    # against the orchestration block. Folding it in would make it un-resolvable.
    spec = crew_entry_to_worker_spec(WorkerCrewEntry(name="w", use="u", mcp="none"), "t")
    assert spec.mcp == "none"
    assert spec.options is None


def test_scope_is_carried_through_to_become_the_scope_guard():
    scope = WorkerScope(write=("src/**",))
    spec = crew_entry_to_worker_spec(WorkerCrewEntry(name="w", use="u", scope=scope), "t")
    assert spec.scope is scope


def test_a_scheme_slash_is_not_mistaken_for_the_host_org_separator():
    # The TS original searches the whole string, so `ssh://` supplies the `/`
    # that makes `git@` look like an `@ref` — and the source resolves to
    # `ssh://git`, which cannot clone. See the comment in `use_to_source`.
    assert use_to_source("ssh://git@host/org/repo") == "ssh://git@host/org/repo"
    assert use_to_source("https://github.com/org/repo@v2") == "https://github.com/org/repo"
