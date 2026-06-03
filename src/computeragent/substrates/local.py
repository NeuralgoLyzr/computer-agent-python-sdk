"""``LocalSubstrate`` — provides a workspace ``cwd`` for the engine.

Resolution rules:

* :class:`pathlib.Path` — used directly (caller-owned; not deleted on close).
* String beginning with ``http``, ``git@``, or ``ssh://`` — git-cloned with
  ``--depth 1`` into a temp dir; the temp dir is removed on close.
* String that looks like a filesystem path — coerced to :class:`Path` and
  used directly.
* Dict (inline manifest) — extracts ``files`` if present; otherwise the
  current working directory is used. (Materialising arbitrary inline
  files is deferred to 0.3.0.)

No subprocess management lives here. ``claude-agent-sdk`` and
``GitAgentEngine`` own their own model-call subprocesses and just need a
``cwd`` to point at.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..protocol_types import IdentitySource, SubstrateHandle

logger = logging.getLogger("computeragent.substrates")


class LocalSubstrate:
    name = "local"

    async def boot(
        self,
        *,
        source: IdentitySource,
        envs: Mapping[str, str],  # noqa: ARG002 - reserved for parity with remote substrates
    ) -> SubstrateHandle:
        if isinstance(source, Path):
            return SubstrateHandle(workspace_path=source, close=_noop_close)

        if isinstance(source, str):
            if _looks_like_git_url(source):
                workspace = await _git_clone(source)
                return SubstrateHandle(
                    workspace_path=workspace,
                    close=_make_rmtree_close(workspace),
                )
            # Plain filesystem path — caller-owned.
            return SubstrateHandle(workspace_path=Path(source), close=_noop_close)

        if isinstance(source, dict):
            # Inline manifest. ``files`` materialisation is parked for 0.3.0;
            # for MVP we just use the caller's cwd (closest analogue of
            # "run the agent here").
            return SubstrateHandle(workspace_path=Path.cwd(), close=_noop_close)

        raise TypeError(
            f"LocalSubstrate.boot: unsupported source type {type(source).__name__}"
        )


_KNOWN_GIT_HOSTS = (
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "git.sr.ht",
    "codeberg.org",
)


def _looks_like_git_url(source: str) -> bool:
    """Heuristic check — does this string look like something ``git clone``
    can fetch?

    Accepts both explicit ``.git`` suffixes and bare known-host URLs
    (e.g. ``https://github.com/owner/repo``) since GitHub-style hosts
    clone fine without the suffix.
    """
    if source.startswith(("git@", "ssh://", "git://")):
        return True
    if source.startswith(("https://", "http://")):
        if source.endswith(".git"):
            return True
        return any(host in source for host in _KNOWN_GIT_HOSTS)
    return False


async def _git_clone(url: str) -> Path:
    workspace = Path(tempfile.mkdtemp(prefix="computeragent-substrate-"))
    proc = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        "--depth",
        "1",
        url,
        str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        shutil.rmtree(workspace, ignore_errors=True)
        raise RuntimeError(
            f"git clone failed for {url!r}: {stderr.decode(errors='replace')!r}"
        )
    logger.debug("cloned %s into %s", url, workspace)
    return workspace


def _make_rmtree_close(path: Path) -> Any:
    async def _close() -> None:
        shutil.rmtree(path, ignore_errors=True)

    return _close


async def _noop_close() -> None:
    return None


__all__ = ["LocalSubstrate"]
