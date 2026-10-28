"""Git as the ground truth for what a worker wrote.

Each worker gets its own git worktree on its own branch off a shared baseline
commit, so ``git diff --name-only`` is that worker's exact, complete record no
matter how the bytes arrived — and worker A's writes can never appear in worker
B's diff under parallel dispatch. The integration merge is a second measurement
for free: two workers whose scopes did not overlap cannot conflict, so a
conflict is direct evidence of an overlap.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .path_jail import assert_safe_path_segment

logger = logging.getLogger("computeragent.orchestration.worktree")

BASELINE_BRANCH = "orchestration-baseline"
INTEGRATION_BRANCH = "orchestration-integration"

#: Build/cache droppings a worker creates merely by running — never its
#: artifacts. Also excludes the worker's own materialised GAP identity files (a
#: synthetic agent.yaml / .claude/ for an inline worker) so they never count as
#: artifacts.
EXCLUDES = (
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    "*.egg-info/",
    ".coverage",
    ".DS_Store",
    "node_modules/",
    "agent.yaml",
    ".claude/",
)

_MAX_OUTPUT = 32 * 1024 * 1024


@dataclass
class WorkerTree:
    worker_id: str
    path: Path
    branch: str
    base_sha: str
    committed: bool = False


class GitError(RuntimeError):
    """A git invocation the caller asked to be checked failed."""


async def _git(cwd: Path, args: list[str], *, check: bool = True) -> str:
    """Run git with identity forced on the command line (no ambient config needed)."""
    full = [
        "git",
        "-c",
        "user.email=orchestrator@example.invalid",
        "-c",
        "user.name=Orchestrator",
        "-c",
        "commit.gpgsign=false",
        "-C",
        str(cwd),
        *args,
    ]
    proc = await asyncio.create_subprocess_exec(
        *full,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        if not check:
            return ""
        detail = stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
        raise GitError(f"git {' '.join(args)} failed in {cwd}: {detail}")
    return stdout[:_MAX_OUTPUT].decode("utf-8", "replace")


async def _git_ok(cwd: Path, args: list[str]) -> bool:
    """Did this git invocation succeed? (No output needed — just the status.)"""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(cwd),
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


def _worktrees_root(repo: Path) -> Path:
    """Worker trees live BESIDE the repo, never inside it (else they'd pollute
    its diff)."""
    return repo.parent / f"{repo.name}.worktrees"


def _lines(out: str) -> list[str]:
    return sorted(s.strip() for s in out.split("\n") if s.strip())


async def init_baseline(repo: Path) -> str:
    """Make ``repo`` a git repository (always explicitly — never via discovery,
    which could resolve to a parent repo) and record the pristine baseline
    commit."""
    repo = Path(repo)
    if not (repo / ".git").exists():
        await _git(repo, ["init", "-b", BASELINE_BRANCH])
    exclude_path = repo / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    exclude_path.write_text("\n".join(EXCLUDES) + "\n", encoding="utf-8")

    await _git(repo, ["add", "-A"])
    # `--allow-empty` so a genuinely empty workspace still yields a base commit.
    await _git(repo, ["commit", "--allow-empty", "-m", "baseline before orchestration"])
    return (await _git(repo, ["rev-parse", "HEAD"])).strip()


async def create_worktree(repo: Path, worker_id: str, base_sha: str) -> WorkerTree:
    # Worker ids are LLM-authored (roster entries / dispatch_many specs) and
    # become both a directory name and a branch name — a single safe segment or
    # nothing.
    assert_safe_path_segment(worker_id, "worker_id")
    repo = Path(repo)
    root = _worktrees_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    path = root / worker_id
    branch = f"orchestration/{worker_id}"
    shutil.rmtree(path, ignore_errors=True)
    # Drop registrations whose directory we just deleted (or that a crashed run
    # left behind), so `add` sees a clean slate. No-op when there are none.
    await _git(repo, ["worktree", "prune"], check=False)
    # `-B`, not `-b`: reset the branch to this wave's baseline if it already
    # exists. A second fan-out in the same session reuses worker ids — the
    # orchestrator's own prompt prescribes `w1`, `w2` — and `cleanup()` removes
    # worker WORKTREES but never their BRANCHES, so `-b` failed with "a branch
    # named 'orchestration/w1' already exists" and the worker came back errored
    # with 0 artifacts. `--force` covered the path collision and masked how much
    # it did not cover.
    #
    # Resetting is also the semantics we want independent of the bug: a wave's
    # worker starts from the pristine baseline, never from whatever the last
    # wave happened to leave on that branch. `--force` additionally waives the
    # "already checked out elsewhere" guard, for the case where cleanup did not
    # get to run.
    await _git(repo, ["worktree", "add", "--force", "-B", branch, str(path), base_sha])
    return WorkerTree(worker_id=worker_id, path=path, branch=branch, base_sha=base_sha)


async def diff_paths(tree: WorkerTree) -> tuple[str, ...]:
    """Every path this worker changed, relative to the baseline. Staging first
    makes untracked files visible."""
    await _git(tree.path, ["add", "-A"])
    out = await _git(tree.path, ["diff", "--cached", "--name-only", tree.base_sha])
    return tuple(_lines(out))


async def commit_worker(tree: WorkerTree, message: str) -> bool:
    """Commit the worker's work. Returns ``False`` when it changed nothing."""
    await _git(tree.path, ["add", "-A"])
    status = (await _git(tree.path, ["status", "--porcelain"])).strip()
    if not status:
        return False
    await _git(tree.path, ["commit", "-m", message])
    tree.committed = True
    return True


@dataclass(frozen=True)
class IntegrationResult:
    integrated: tuple[str, ...] = ()
    conflicts: dict[str, tuple[str, ...]] = field(default_factory=dict)


async def integrate(repo: Path, trees: list[WorkerTree], base_sha: str) -> IntegrationResult:
    """Merge each committed worker branch into an integration branch off the
    baseline.

    A conflicting branch is aborted and recorded rather than force-resolved —
    the conflict itself is the finding worth reporting.
    """
    repo = Path(repo)
    await _git(repo, ["checkout", "-B", INTEGRATION_BRANCH, base_sha])
    conflicts: dict[str, tuple[str, ...]] = {}
    for tree in trees:
        if not tree.committed:
            continue
        try:
            await _git(repo, ["merge", "--no-edit", "--no-ff", tree.branch])
        except GitError:
            clashing = tuple(
                _lines(
                    await _git(
                        repo,
                        ["diff", "--name-only", "--diff-filter=U"],
                        check=False,
                    )
                )
            )
            conflicts[tree.worker_id] = clashing or ("<merge failed with no unmerged paths>",)
            await _git(repo, ["merge", "--abort"], check=False)
    integrated = tuple(_lines(await _git(repo, ["diff", "--name-only", base_sha])))
    return IntegrationResult(integrated=integrated, conflicts=conflicts)


async def cleanup(repo: Path, trees: list[WorkerTree]) -> None:
    """Remove worker trees. Best effort — never fail a run over cleanup."""
    repo = Path(repo)
    for tree in trees:
        await _git(repo, ["worktree", "remove", "--force", str(tree.path)], check=False)
    shutil.rmtree(_worktrees_root(repo), ignore_errors=True)


async def restore_baseline_paths(tree: WorkerTree, rel_paths: tuple[str, ...]) -> None:
    """Undo the identity loader's own writes inside a worker's worktree.

    An inline worker's identity is materialised INTO its worktree (the loader's
    workdir and the worker's work surface are the same directory), so loading it
    overwrites the orchestrator repo's ``agent.yaml`` with a synthetic 2-line
    stub. That single write was catastrophic on the DEFAULT config:

    * every worker's branch modified ``agent.yaml`` with DIFFERENT content
      (``name: w1`` vs ``name: w2``), so any wave with ≥2 workers conflicted on
      it no matter what the task was;
    * ``git merge --abort`` is atomic, so the second worker's ENTIRE diff was
      discarded — including the only real deliverable it produced;
    * the integrated tree ended up with the agent's own manifest replaced by the
      stub (observed: 48 lines → 2);
    * and the scope-guard could not prevent any of it, because it gates Write
      and Edit TOOL calls while this write comes from the loader.

    Restoring the path to its baseline content (or deleting it, when the
    baseline had no such file) leaves the worker's diff containing only the
    worker's own work. Best-effort: a failure here must not lose the worker.
    """
    for rel in rel_paths:
        # Did the baseline have this file at all? A non-GAP repo may not, in
        # which case the loader CREATED it and the right undo is to delete it.
        if await _git_ok(tree.path, ["cat-file", "-e", f"{tree.base_sha}:{rel}"]):
            await _git(tree.path, ["checkout", tree.base_sha, "--", rel], check=False)
        else:
            target = tree.path / rel
            try:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            except OSError:
                logger.debug("restore_baseline_paths: could not remove %s", target)


__all__ = [
    "BASELINE_BRANCH",
    "EXCLUDES",
    "INTEGRATION_BRANCH",
    "GitError",
    "IntegrationResult",
    "WorkerTree",
    "cleanup",
    "commit_worker",
    "create_worktree",
    "diff_paths",
    "init_baseline",
    "integrate",
    "restore_baseline_paths",
]
