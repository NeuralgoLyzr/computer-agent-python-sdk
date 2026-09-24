"""Cloning a source over a non-HTTPS protocol.

The substrate branches on the URL shape, and the `git@` / `ssh://` / `git://`
branch had never been exercised because testing it seemed to need a credential.
It does not: `git daemon` serves a local bare repo over the git protocol with no
auth at all, which walks exactly the same code path.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from computeragent.substrates import LocalSubstrate
from computeragent.substrates.local import _looks_like_git_url

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _run(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_daemon(tmp_path: Path):
    """A bare repo with one commit, served over `git://` on a free port."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "README.md").write_text("served over the git protocol\n", encoding="utf-8")
    _run("git", "init", "-q", "-b", "main", str(work))
    _run("git", "-C", str(work), "config", "user.email", "t@example.invalid")
    _run("git", "-C", str(work), "config", "user.name", "T")
    _run("git", "-C", str(work), "add", "-A")
    _run("git", "-C", str(work), "commit", "-qm", "init")

    bare = tmp_path / "repo.git"
    _run("git", "clone", "-q", "--bare", str(work), str(bare))
    # `git daemon` refuses to serve a repo without this marker unless
    # --export-all is passed; being explicit is the smaller hammer.
    (bare / "git-daemon-export-ok").touch()

    port = _free_port()
    proc = subprocess.Popen(
        [
            "git",
            "daemon",
            f"--port={port}",
            f"--base-path={tmp_path}",
            "--reuseaddr",
            "--informative-errors",
            str(tmp_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait for the listener rather than sleeping a fixed amount.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.05)
    else:  # pragma: no cover - only on a wedged daemon
        proc.terminate()
        pytest.skip("git daemon did not start")

    try:
        yield f"git://127.0.0.1:{port}/repo.git"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_git_protocol_urls_take_the_clone_branch():
    assert _looks_like_git_url("git://host/repo.git")
    assert _looks_like_git_url("ssh://git@host/repo.git")
    assert _looks_like_git_url("git@github.com:org/repo.git")


async def test_clones_over_the_git_protocol(git_daemon):
    handle = await LocalSubstrate().boot(source=git_daemon, envs={})
    try:
        assert (handle.workspace_path / "README.md").exists()
        assert handle.revision, "the cloned commit SHA should be reported"
    finally:
        await handle.close()


async def test_the_temp_clone_is_removed_on_close(git_daemon):
    handle = await LocalSubstrate().boot(source=git_daemon, envs={})
    workspace = handle.workspace_path

    await handle.close()

    assert not workspace.exists()


async def test_an_unreachable_git_url_fails_loudly(tmp_path):
    """Not silently treated as a filesystem path — that is how a private-host
    URL used to become a nonexistent directory with no error at all."""
    port = _free_port()  # nothing listening
    with pytest.raises(Exception) as excinfo:
        await asyncio.wait_for(
            LocalSubstrate().boot(source=f"git://127.0.0.1:{port}/repo.git", envs={}),
            timeout=60,
        )

    assert "clone" in str(excinfo.value).lower()
