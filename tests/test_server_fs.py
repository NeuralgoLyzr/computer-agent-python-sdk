"""The app server's workspace file routes, confined to the workspace."""

from __future__ import annotations

import os

import pytest
import respx

from server_helpers import FakeEngine, client_for, make_app, mock_backends, session_body


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)  # the agent's workspace is the working directory
    return ws


async def start(client):
    r = await client.post("/v1/sessions", json=session_body())
    assert r.status_code == 201


@respx.mock
async def test_fs_routes(agent_doc, workspace):
    mock_backends(agent_doc)
    async with client_for(make_app(FakeEngine())) as client:
        await start(client)
        base = "/v1/sessions/s1/fs"

        r = await client.put(f"{base}/file", params={"path": "docs/a.txt"}, content=b"hello world")
        assert r.json() == {"ok": True, "size": 11}
        assert (
            await client.get(f"{base}/file", params={"path": "docs/a.txt"})
        ).content == b"hello world"

        r = await client.post(
            f"{base}/edit", json={"path": "docs/a.txt", "oldString": "world", "newString": "there"}
        )
        assert r.json() == {"ok": True, "replacements": 1}
        r = await client.post(
            f"{base}/edit", json={"path": "docs/a.txt", "oldString": "zzz", "newString": ""}
        )
        assert (r.status_code, r.json()["code"]) == (422, "WORKDIR_EDIT_TARGET_NOT_FOUND")

        assert (
            await client.post(f"{base}/mkdir", json={"path": "out/deep", "recursive": True})
        ).json() == {"ok": True}
        assert (
            await client.post(f"{base}/move", json={"from": "docs/a.txt", "to": "out/b.txt"})
        ).json() == {"ok": True}

        tree = (await client.get(f"{base}/tree", params={"depth": "2"})).json()["entries"]
        assert {(e["path"], e["type"]) for e in tree} >= {
            ("out", "dir"),
            ("out/b.txt", "file"),
            ("docs", "dir"),
        }
        entry = next(e for e in tree if e["path"] == "out/b.txt")
        assert (
            entry["size"] == 11
            and isinstance(entry["mtime"], int)
            and isinstance(entry["mode"], int)
        )

        assert (await client.delete(f"{base}/file", params={"path": "out/b.txt"})).json() == {
            "ok": True
        }
        assert not (workspace / "out" / "b.txt").exists()
        r = await client.get(f"{base}/file", params={"path": "out/b.txt"})
        assert (r.status_code, r.json()["code"]) == (404, "WORKDIR_FILE_NOT_FOUND")
        r = await client.get(f"{base}/file")
        assert (r.status_code, r.json()["code"]) == (400, "QUERY_PARAM_MISSING")


@respx.mock
async def test_a_non_empty_directory_is_never_removed(agent_doc, workspace):
    mock_backends(agent_doc)
    (workspace / "keep").mkdir()
    (workspace / "keep" / "f.txt").write_text("x")
    async with client_for(make_app(FakeEngine())) as client:
        await start(client)
        r = await client.delete(
            "/v1/sessions/s1/fs/file", params={"path": "keep", "recursive": "true"}
        )

    assert (r.status_code, r.json()["code"]) == (409, "WORKDIR_DIRECTORY_NOT_EMPTY")
    assert (workspace / "keep" / "f.txt").exists()


@pytest.mark.parametrize(
    "path", ["../outside.txt", "/etc/passwd", "a/../../x", "nul\0byte", "link/secret"]
)
@respx.mock
async def test_a_path_escaping_the_workspace_is_refused(agent_doc, workspace, tmp_path, path):
    mock_backends(agent_doc)
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "secret").write_text("s")
    os.symlink(tmp_path / "secret", workspace / "link")
    async with client_for(make_app(FakeEngine())) as client:
        await start(client)
        r = await client.get("/v1/sessions/s1/fs/file", params={"path": path})

    assert (r.status_code, r.json()["code"]) == (400, "PATH_ESCAPE_REJECTED")
