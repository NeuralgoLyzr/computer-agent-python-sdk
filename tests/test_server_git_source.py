"""A session's git source (``identity.source``) replaces the image's baked-in agent."""

from __future__ import annotations

import base64
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import respx

import computeragent.server.app as server_app
from server_helpers import FakeEngine, client_for, make_app, mock_backends, session_body

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
DEAD = "http://127.0.0.1:9"  # nothing listens on port 9


def make_repo(root: Path, files: dict[str, str]) -> str:
    """A committed git repository holding ``files``; its ``file://`` URL."""
    root.mkdir()
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", *GIT_ID, "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", *GIT_ID, "-C", str(root), "commit", "-qm", "init"], check=True)
    return root.as_uri()


def git_session(url: str, **envs: str) -> dict:
    body = session_body(
        identity={"loader": "gitagentprotocol", "source": {"type": "git", "url": url}}
    )
    body["envs"] = {**(body.get("envs") or {}), **envs}
    return body


@pytest.fixture
def git_host() -> Iterator[tuple[str, list[tuple[str, str | None]]]]:
    """An HTTP server that records each git request (path, Authorization) and 404s."""
    seen: list[tuple[str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append((self.path, self.headers.get("Authorization")))
            self.send_response(404)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *_a: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.delenv("COMPUTER_AGENT_EGRESS_PROXY_SECRET", raising=False)
    monkeypatch.setattr(server_app, "SOURCES_ROOT", tmp_path / "sources")
    baked = tmp_path / "baked"  # the image's own GAP agent
    baked.mkdir()
    (baked / "agent.yaml").write_text("name: baked\n", encoding="utf-8")
    monkeypatch.setenv("COMPUTER_AGENT_GAP_DIR", str(baked))


@respx.mock
async def test_a_git_source_with_an_agent_yaml_replaces_the_baked_in_agent(agent_doc, tmp_path):
    mock_backends(agent_doc)
    url = make_repo(tmp_path / "repo", {"agent.yaml": "name: from-git\n", "SOUL.md": "hi\n"})
    calls: list = []
    async with client_for(make_app(FakeEngine(), calls)) as client:
        r = await client.post("/v1/sessions", json=git_session(url))
        clone = tmp_path / "sources" / "s1"

        assert r.status_code == 201
        assert calls[0]["gap_dir"] == clone
        assert (clone / "agent.yaml").read_text(encoding="utf-8") == "name: from-git\n"
        tree = (await client.get("/v1/sessions/s1/fs/tree")).json()["entries"]
        assert {e["path"] for e in tree} >= {"agent.yaml", "SOUL.md"}

        await client.delete("/v1/sessions/s1")

    assert not clone.exists()  # removed with the session


@respx.mock
async def test_a_git_source_without_an_agent_yaml_is_the_workspace(agent_doc, tmp_path):
    mock_backends(agent_doc)
    url = make_repo(tmp_path / "repo", {"README.md": "hello\n"})
    calls: list = []
    async with client_for(make_app(FakeEngine(), calls)) as client:
        r = await client.post("/v1/sessions", json=git_session(url))
        tree = (await client.get("/v1/sessions/s1/fs/tree")).json()["entries"]

    assert r.status_code == 201
    assert calls[0]["workspace"] == tmp_path / "sources" / "s1"
    assert "gap_dir" not in calls[0]
    assert "README.md" in {e["path"] for e in tree}  # not the baked agent's directory


@respx.mock
@pytest.mark.parametrize(
    "identity",
    [
        {"loader": "gitagentprotocol", "source": {"type": "local", "path": "/agent"}},
        {"loader": "gitagentprotocol", "source": {"type": "git", "url": "  "}},
    ],
)
async def test_without_a_git_url_the_baked_in_agent_runs(agent_doc, tmp_path, identity):
    mock_backends(agent_doc)
    calls: list = []
    async with client_for(make_app(FakeEngine(), calls)) as client:
        r = await client.post("/v1/sessions", json=session_body(identity=identity))
        tree = (await client.get("/v1/sessions/s1/fs/tree")).json()["entries"]

    assert r.status_code == 201
    assert "gap_dir" not in calls[0] and "workspace" not in calls[0]
    assert [e["path"] for e in tree] == ["agent.yaml"]  # the baked GAP directory
    assert not (tmp_path / "sources").exists()


@respx.mock
@pytest.mark.parametrize(
    "url",
    [
        "file:///nonexistent/repo.git",
        "http://user:s3cret-token@127.0.0.1:9/acme/app.git",  # nothing listens on port 9
    ],
)
async def test_a_failed_clone_fails_the_session(agent_doc, tmp_path, url):
    mock_backends(agent_doc)
    calls: list = []
    async with client_for(make_app(FakeEngine(), calls)) as client:
        r = await client.post("/v1/sessions", json=git_session(url))
        status = await client.get("/v1/sessions/s1")

    assert r.status_code == 502
    assert r.json()["code"] == "AGENT_SOURCE_FETCH_FAILED"
    assert "s3cret-token" not in r.text
    assert calls == []  # the baked-in agent is not run instead
    assert status.status_code == 404
    assert not (tmp_path / "sources" / "s1").exists()


@respx.mock
@pytest.mark.parametrize("source", ["github.com/acme/app", "https://github.com/acme/app.git"])
async def test_the_git_egress_route_hosts_the_clone_with_the_sandbox_token(
    agent_doc, git_host, source
):
    mock_backends(agent_doc)
    host, seen = git_host
    body = git_session(source, COMPUTER_AGENT_EGRESS_GIT_URL=f"{host}/git/v1/")
    async with client_for(make_app(FakeEngine())) as client:
        r = await client.post("/v1/sessions", json=body)

    assert r.json()["code"] == "AGENT_SOURCE_FETCH_FAILED"  # the recorder 404s
    path, auth = seen[0]
    assert path == "/git/v1/github.com/acme/app.git/info/refs?service=git-upload-pack"
    assert auth == "Basic " + base64.b64encode(b"x-access-token:sbt_1").decode()


@respx.mock
async def test_the_images_git_egress_route_wins_over_the_sessions(agent_doc, git_host, monkeypatch):
    mock_backends(agent_doc)
    host, seen = git_host
    monkeypatch.setenv("COMPUTER_AGENT_EGRESS_GIT_URL", f"{host}/git/v1")
    body = git_session("github.com/acme/app", COMPUTER_AGENT_EGRESS_GIT_URL=f"{DEAD}/git/v1")
    async with client_for(make_app(FakeEngine())) as client:
        await client.post("/v1/sessions", json=body)

    assert seen and seen[0][0].startswith("/git/v1/github.com/acme/app.git/")


@respx.mock
async def test_without_a_git_egress_route_the_url_is_cloned_as_is(agent_doc, git_host):
    mock_backends(agent_doc)
    host, seen = git_host
    async with client_for(make_app(FakeEngine())) as client:
        await client.post("/v1/sessions", json=git_session(f"{host}/acme/app.git"))

    assert seen[0] == ("/acme/app.git/info/refs?service=git-upload-pack", None)  # no token


@respx.mock
async def test_a_source_that_is_not_host_owner_repo_is_refused_behind_the_egress(agent_doc):
    mock_backends(agent_doc)
    body = git_session("github.com/acme", COMPUTER_AGENT_EGRESS_GIT_URL=f"{DEAD}/git/v1")
    async with client_for(make_app(FakeEngine())) as client:
        r = await client.post("/v1/sessions", json=body)

    assert r.status_code == 502
    assert "<host>/<owner>/<repo>" in r.json()["detail"]
