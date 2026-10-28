"""Coverage for ``LocalSubstrate`` — pass-through only; nothing is ever cloned."""

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
async def test_a_url_string_is_never_cloned():
    """The agent is baked into the image: a URL-looking string is a path, and
    no subprocess (git or otherwise) is ever started."""
    sub = LocalSubstrate()

    async def _no_exec(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("LocalSubstrate must not start a subprocess")

    with patch("asyncio.create_subprocess_exec", _no_exec):
        handle = await sub.boot(source="https://github.com/example/demo.git", envs={})
    assert handle.workspace_path == Path("https://github.com/example/demo.git")
    await handle.close()


def test_registry_resolves_local():
    sub = resolve_substrate("local")
    assert sub.name == "local"
    assert isinstance(sub, LocalSubstrate)
