"""Git as the ground truth for what each worker wrote. Real ``git``, no network."""

from __future__ import annotations

from pathlib import Path

import pytest

from computeragent.orchestration.path_jail import PathEscapeError
from computeragent.orchestration.worktree import (
    INTEGRATION_BRANCH,
    cleanup,
    commit_worker,
    create_worktree,
    diff_paths,
    init_baseline,
    integrate,
    restore_baseline_paths,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    # A workspace, NOT already a git repo — `init_baseline` must make it one.
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    (workdir / "agent.yaml").write_text("name: orchestrator\nversion: '1'\n")
    (workdir / "README.md").write_text("hello\n")
    return workdir


async def test_baseline_initialises_a_repo_and_returns_its_sha(repo: Path):
    sha = await init_baseline(repo)
    assert (repo / ".git").exists()
    assert len(sha) == 40


async def test_baseline_is_idempotent_on_an_existing_repo(repo: Path):
    first = await init_baseline(repo)
    (repo / "new.txt").write_text("x")
    second = await init_baseline(repo)
    assert first != second  # the second baseline captured the new file


async def test_an_empty_workspace_still_yields_a_base_commit(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert len(await init_baseline(empty)) == 40


async def test_worktrees_live_beside_the_repo_never_inside_it(repo: Path):
    sha = await init_baseline(repo)
    tree = await create_worktree(repo, "w1", sha)
    assert tree.path.parent.name == "workspace.worktrees"
    assert repo not in tree.path.parents
    # …so they never pollute the orchestrator's own diff.
    assert not (repo / "workspace.worktrees").exists()


async def test_a_worker_id_that_is_not_a_safe_segment_is_refused(repo: Path):
    sha = await init_baseline(repo)
    with pytest.raises(PathEscapeError):
        await create_worktree(repo, "../escape", sha)


async def test_the_diff_is_exactly_what_this_worker_wrote(repo: Path):
    sha = await init_baseline(repo)
    tree = await create_worktree(repo, "w1", sha)
    (tree.path / "src").mkdir()
    (tree.path / "src" / "new.py").write_text("print()\n")
    (tree.path / "README.md").write_text("changed\n")
    assert await diff_paths(tree) == ("README.md", "src/new.py")


async def test_build_droppings_are_excluded_from_attribution(repo: Path):
    sha = await init_baseline(repo)
    tree = await create_worktree(repo, "w1", sha)
    (tree.path / "__pycache__").mkdir()
    (tree.path / "__pycache__" / "x.pyc").write_text("junk")
    (tree.path / "real.py").write_text("x")
    assert await diff_paths(tree) == ("real.py",)


async def test_one_workers_writes_never_appear_in_anothers_diff(repo: Path):
    sha = await init_baseline(repo)
    a = await create_worktree(repo, "wa", sha)
    b = await create_worktree(repo, "wb", sha)
    (a.path / "a.txt").write_text("a")
    (b.path / "b.txt").write_text("b")
    assert await diff_paths(a) == ("a.txt",)
    assert await diff_paths(b) == ("b.txt",)


async def test_a_second_wave_can_reuse_a_worker_id(repo: Path):
    # `-B`, not `-b`: cleanup removes worktrees but never their branches, so a
    # second fan-out in the same session used to fail with "a branch named
    # 'orchestration/w1' already exists" and the worker came back errored.
    sha = await init_baseline(repo)
    first = await create_worktree(repo, "w1", sha)
    (first.path / "stale.txt").write_text("from wave 1")
    await commit_worker(first, "w1: wave 1")
    await cleanup(repo, [first])

    second = await create_worktree(repo, "w1", sha)
    # …and it starts from the pristine baseline, not from wave 1's leftovers.
    assert not (second.path / "stale.txt").exists()


async def test_committing_nothing_reports_false(repo: Path):
    sha = await init_baseline(repo)
    tree = await create_worktree(repo, "w1", sha)
    assert await commit_worker(tree, "w1: nothing") is False
    assert tree.committed is False


async def test_non_overlapping_workers_merge_cleanly(repo: Path):
    sha = await init_baseline(repo)
    a = await create_worktree(repo, "wa", sha)
    b = await create_worktree(repo, "wb", sha)
    (a.path / "a.txt").write_text("a\n")
    (b.path / "b.txt").write_text("b\n")
    await commit_worker(a, "wa")
    await commit_worker(b, "wb")

    merged = await integrate(repo, [a, b], sha)
    assert merged.conflicts == {}
    assert set(merged.integrated) == {"a.txt", "b.txt"}


async def test_a_scope_overlap_shows_up_as_a_merge_conflict(repo: Path):
    # Two workers whose scopes did not overlap cannot conflict, so a conflict
    # IS the finding.
    sha = await init_baseline(repo)
    a = await create_worktree(repo, "wa", sha)
    b = await create_worktree(repo, "wb", sha)
    (a.path / "README.md").write_text("written by A\n")
    (b.path / "README.md").write_text("written by B\n")
    await commit_worker(a, "wa")
    await commit_worker(b, "wb")

    merged = await integrate(repo, [a, b], sha)
    assert "wb" in merged.conflicts
    assert merged.conflicts["wb"] == ("README.md",)
    # The first branch still merged — one conflict does not lose the wave.
    assert "README.md" in merged.integrated


async def test_an_uncommitted_tree_is_skipped_by_the_merge(repo: Path):
    sha = await init_baseline(repo)
    tree = await create_worktree(repo, "w1", sha)
    merged = await integrate(repo, [tree], sha)
    assert merged.integrated == ()
    assert merged.conflicts == {}


async def test_the_integration_branch_is_reset_to_the_baseline_each_wave(repo: Path):
    sha = await init_baseline(repo)
    await integrate(repo, [], sha)
    head = (repo / ".git" / "HEAD").read_text()
    assert INTEGRATION_BRANCH in head


async def test_cleanup_removes_every_worker_tree(repo: Path):
    sha = await init_baseline(repo)
    a = await create_worktree(repo, "wa", sha)
    await cleanup(repo, [a])
    assert not a.path.exists()
    assert not a.path.parent.exists()


async def test_restoring_a_baseline_path_undoes_a_write_the_worker_did_not_make(
    repo: Path,
):
    sha = await init_baseline(repo)
    tree = await create_worktree(repo, "w1", sha)
    (tree.path / "agent.yaml").write_text("name: w1\nversion: '0'\n")
    (tree.path / "real.py").write_text("work\n")

    await restore_baseline_paths(tree, ("agent.yaml",))
    # Only the worker's own work is left in its diff — which is what stops a
    # wave of ≥2 workers conflicting on a file none of them touched.
    assert await diff_paths(tree) == ("real.py",)


async def test_a_path_the_baseline_never_had_is_deleted_instead(tmp_path: Path):
    workdir = tmp_path / "plain"
    workdir.mkdir()
    (workdir / "README.md").write_text("hi\n")
    sha = await init_baseline(workdir)
    tree = await create_worktree(workdir, "w1", sha)
    (tree.path / "agent.yaml").write_text("name: w1\n")

    await restore_baseline_paths(tree, ("agent.yaml",))
    assert not (tree.path / "agent.yaml").exists()


async def test_restoring_a_path_nobody_touched_is_a_no_op(repo: Path):
    sha = await init_baseline(repo)
    tree = await create_worktree(repo, "w1", sha)
    await restore_baseline_paths(tree, ("agent.yaml", "does-not-exist.txt"))
    assert await diff_paths(tree) == ()
