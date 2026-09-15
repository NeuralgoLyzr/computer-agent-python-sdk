"""Coverage for private-GAP auth: the resolve client, env-injected git auth
(token never in argv), and SHA capture on clone."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from computeragent.harness.git_credential_client import resolve_git_credential
from computeragent.substrates import LocalSubstrate
from computeragent.substrates.local import _clone_env, _git_clone, _host_of


# ── _host_of ────────────────────────────────────────────────────────────────
def test_host_of_handles_every_url_shape():
    assert _host_of("https://github.com/o/r") == "github.com"
    assert _host_of("git@github.com:o/r.git") == "github.com"
    assert _host_of("ssh://git@GitHub.com/o/r") == "github.com"
    assert _host_of("https://gitlab.example.com:443/o/r") == "gitlab.example.com"


# ── env-injected auth (no token in argv) ─────────────────────────────────────
def test_clone_env_injects_auth_header_via_git_config():
    env = _clone_env(
        "https://github.com/o/r.git",
        {"username": "x-access-token", "token": "ghp_secret", "host": "github.com"},
    )
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
    scheme, b64 = env["GIT_CONFIG_VALUE_0"].split("Basic ")
    assert scheme == "Authorization: "
    assert base64.b64decode(b64).decode() == "x-access-token:ghp_secret"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_clone_env_no_credential_no_injection():
    env = _clone_env("https://github.com/o/r", None)
    assert "GIT_CONFIG_COUNT" not in env


def test_clone_env_ssh_url_not_injected():
    # SSH authenticates by key — an HTTP header is meaningless, so skip it.
    env = _clone_env("git@github.com:o/r", {"token": "ghp_x", "username": "x-access-token"})
    assert "GIT_CONFIG_COUNT" not in env


# ── boot threads the credential + captures the SHA ───────────────────────────
@pytest.mark.asyncio
async def test_boot_injects_credential_and_captures_revision():
    sub = LocalSubstrate()
    captured: dict = {}

    class _Proc:
        def __init__(self, out: bytes = b"", rc: int = 0):
            self._out = out
            self.returncode = rc

        async def communicate(self):
            return (self._out, b"")

    async def _fake_exec(*args, **kwargs):
        if "rev-parse" in args:
            return _Proc(out=b"abc123def456\n")
        captured["env"] = kwargs.get("env")
        captured["argv"] = args
        return _Proc()

    with patch("asyncio.create_subprocess_exec", _fake_exec):
        handle = await sub.boot(
            source="https://github.com/example/private.git",
            envs={},
            git_credential={"username": "x-access-token", "token": "ghp_tok", "host": "github.com"},
        )
    # SHA captured onto the handle for the coordinator to stamp.
    assert handle.revision == "abc123def456"
    # Auth rode in via env config — never in argv.
    assert "ghp_tok" not in " ".join(str(a) for a in captured["argv"])
    assert captured["env"]["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    await handle.close()
    assert not handle.workspace_path.exists()


# ── resolve client (httpx mocked) ────────────────────────────────────────────
class _FakeResp:
    def __init__(self, status: int, data=None):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class _FakeClient:
    resp: _FakeResp = _FakeResp(200)
    last: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.last = {"url": url, "headers": headers or {}, "json": json or {}}
        return _FakeClient.resp


@pytest.mark.asyncio
async def test_resolve_returns_credential_on_200(monkeypatch):
    monkeypatch.setenv("AGENTOS_API_URL", "https://h/agentos/api/v1")
    monkeypatch.setenv("COMPUTERAGENT_HARNESS_TOKEN", "cak_test")
    _FakeClient.resp = _FakeResp(
        200, {"host": "github.com", "username": "x-access-token", "token": "ghp_resolved"}
    )
    with patch("httpx.AsyncClient", _FakeClient):
        out = await resolve_git_credential("https://github.com/o/r")
    assert out == {"host": "github.com", "username": "x-access-token", "token": "ghp_resolved"}
    assert _FakeClient.last["url"] == "https://h/agentos/api/v1/git-credentials/resolve"
    assert _FakeClient.last["headers"]["authorization"] == "Bearer cak_test"
    assert _FakeClient.last["json"] == {"repoUrl": "https://github.com/o/r"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 401, 403])
async def test_resolve_returns_none_on_miss(monkeypatch, status):
    monkeypatch.setenv("AGENTOS_API_URL", "https://h/agentos/api/v1")
    monkeypatch.setenv("COMPUTERAGENT_HARNESS_TOKEN", "cak_test")
    _FakeClient.resp = _FakeResp(status, {"error": {"code": "NO_CREDENTIAL"}})
    with patch("httpx.AsyncClient", _FakeClient):
        assert await resolve_git_credential("https://github.com/o/r") is None


@pytest.mark.asyncio
async def test_resolve_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("AGENTOS_API_URL", raising=False)
    monkeypatch.delenv("COMPUTERAGENT_HARNESS_TOKEN", raising=False)
    monkeypatch.delenv("AGENTOS_INGEST_TOKEN", raising=False)
    # No network call should happen — patch to explode if it does.
    with patch("httpx.AsyncClient", side_effect=AssertionError("should not be called")):
        assert await resolve_git_credential("https://github.com/o/r") is None


# ── real git: the auth header we inject must be honored by the installed git ──
# This is the load-bearing check — GIT_CONFIG_COUNT needs git >= 2.31, so a
# silent no-op here (older git) is exactly the "PAT clone fails" failure mode.
def test_installed_git_honors_injected_extra_header():
    cred = {"username": "x-access-token", "token": "ghp_dummytoken", "host": "github.com"}
    env = _clone_env("https://github.com/o/r.git", cred)
    out = subprocess.run(
        ["git", "config", "--get", "http.https://github.com/.extraHeader"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, (
        "installed git ignored GIT_CONFIG_* env injection — PAT auth won't apply "
        f"(need git >= 2.31; have: {subprocess.run(['git', '--version'], capture_output=True, text=True).stdout.strip()})"
    )
    line = out.stdout.strip()
    assert line.startswith("Authorization: Basic ")
    assert base64.b64decode(line.split("Basic ")[1]).decode() == "x-access-token:ghp_dummytoken"


def _seed_bare_repo(tmp_path: Path) -> Path:
    """A local bare repo with one commit (no network, no auth)."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "seed"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    run = lambda *a: subprocess.run(a, check=True, capture_output=True, env=env)  # noqa: E731
    run("git", "init", "--bare", "-q", str(origin))
    run("git", "init", "-q", str(work))
    (work / "agent.yaml").write_text("name: demo\n")
    run("git", "-C", str(work), "add", ".")
    run("git", "-C", str(work), "commit", "-q", "-m", "init")
    run("git", "-C", str(work), "branch", "-M", "main")
    run("git", "-C", str(work), "remote", "add", "origin", str(origin))
    run("git", "-C", str(work), "push", "-q", "origin", "main")
    # Point the bare repo's HEAD at main. Without this the bare repo's default
    # HEAD (master, per init.defaultBranch) doesn't match the pushed branch, so
    # `git clone` checks out nothing and the working tree is empty.
    run("git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main")
    return origin


@pytest.mark.asyncio
async def test_git_clone_real_repo_captures_sha(tmp_path: Path):
    """End-to-end clone + rev-parse against a real (local) repo using the real
    git binary — proves the boot→clone→revision plumbing, independent of auth."""
    origin = _seed_bare_repo(tmp_path)
    workspace, revision = await _git_clone(f"file://{origin}")
    try:
        assert (workspace / "agent.yaml").exists()
        assert revision and re.fullmatch(r"[0-9a-f]{40}", revision)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
