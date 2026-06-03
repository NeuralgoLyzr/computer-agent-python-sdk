"""Coverage for ``LocalSubstrate`` — pass-through, git clone, cleanup."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from computeragent.substrates import LocalSubstrate, resolve_substrate


@pytest.mark.asyncio
async def test_path_source_returns_same_path(tmp_path: Path):
    sub = LocalSubstrate()
    handle = await sub.boot(source=tmp_path, envs={})
    assert handle.workspace_path == tmp_path
    # close() must be idempotent and must NOT delete a caller-supplied path.
    await handle.close()
    assert tmp_path.exists()


@pytest.mark.asyncio
async def test_filesystem_string_source(tmp_path: Path):
    sub = LocalSubstrate()
    handle = await sub.boot(source=str(tmp_path), envs={})
    assert handle.workspace_path == Path(str(tmp_path))
    await handle.close()
    assert tmp_path.exists()


@pytest.mark.asyncio
async def test_inline_dict_source_falls_back_to_cwd():
    sub = LocalSubstrate()
    handle = await sub.boot(source={"type": "inline", "name": "demo"}, envs={})
    assert handle.workspace_path == Path.cwd()
    await handle.close()


@pytest.mark.asyncio
async def test_unsupported_source_raises():
    sub = LocalSubstrate()
    with pytest.raises(TypeError):
        await sub.boot(source=12345, envs={})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_git_clone_creates_temp_dir_and_close_removes_it():
    """Mock subprocess so the test runs offline."""
    sub = LocalSubstrate()

    async def _fake_communicate(self):  # noqa: ARG001 - signature compat
        return (b"", b"")

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"", b"")

    async def _fake_exec(*args, **kwargs):  # noqa: ARG001
        return _FakeProc()

    with patch("asyncio.create_subprocess_exec", _fake_exec):
        handle = await sub.boot(
            source="https://github.com/example/demo.git",
            envs={},
        )
    assert handle.workspace_path.exists()
    assert "computeragent-substrate-" in handle.workspace_path.name
    cloned = handle.workspace_path
    await handle.close()
    assert not cloned.exists()


@pytest.mark.asyncio
async def test_git_clone_failure_cleans_up_temp_dir():
    sub = LocalSubstrate()

    class _FailingProc:
        returncode = 128

        async def communicate(self):
            return (b"", b"fatal: repository not found")

    async def _fake_exec(*args, **kwargs):  # noqa: ARG001
        return _FailingProc()

    with patch("asyncio.create_subprocess_exec", _fake_exec):  # noqa: SIM117
        with pytest.raises(RuntimeError, match="git clone failed"):
            await sub.boot(
                source="https://github.com/example/missing.git",
                envs={},
            )


def test_registry_resolves_local():
    sub = resolve_substrate("local")
    assert sub.name == "local"
    assert isinstance(sub, LocalSubstrate)
