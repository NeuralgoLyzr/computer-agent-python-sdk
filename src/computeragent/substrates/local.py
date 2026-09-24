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
import atexit
import base64
import logging
import os
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
        git_credential: Mapping[str, str] | None = None,
    ) -> SubstrateHandle:
        if isinstance(source, Path):
            return SubstrateHandle(workspace_path=source, close=_noop_close)

        if isinstance(source, str):
            if _looks_like_git_url(source):
                workspace, revision = await _git_clone(source, git_credential)
                return SubstrateHandle(
                    workspace_path=workspace,
                    close=_make_rmtree_close(workspace),
                    revision=revision,
                )
            # Plain filesystem path — caller-owned.
            return SubstrateHandle(workspace_path=Path(source), close=_noop_close)

        if isinstance(source, dict):
            # Inline manifest. ``files`` materialisation is parked for 0.3.0;
            # for MVP we just use the caller's cwd (closest analogue of
            # "run the agent here").
            return SubstrateHandle(workspace_path=Path.cwd(), close=_noop_close)

        raise TypeError(f"LocalSubstrate.boot: unsupported source type {type(source).__name__}")


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


def _host_of(url: str) -> str:
    """Bare host from a clone URL (for credential scoping + safe logging)."""
    s = url
    if "://" in s:
        s = s.split("://", 1)[1]
    if "@" in s:
        s = s.split("@", 1)[1]
    # host ends at the first '/' or ':' (path or port)
    for sep in ("/", ":"):
        if sep in s:
            s = s.split(sep, 1)[0]
    return s.lower()


def _clone_env(url: str, credential: Mapping[str, str] | None) -> dict[str, str]:
    """Build the subprocess env. When a credential is present and the URL is
    HTTPS, inject the auth as an ``http.<host>.extraHeader`` via ``GIT_CONFIG_*``
    so the token never appears in argv (or the URL). git >= 2.31."""
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")  # never block on a credential prompt
    token = (credential or {}).get("token")
    if credential and token and url.startswith("https://"):
        host = (credential.get("host") or _host_of(url)).lower()
        username = credential.get("username") or "x-access-token"
        basic = base64.b64encode(f"{username}:{token}".encode()).decode()
        # Disable the system gitconfig so platform credential helpers
        # (osxkeychain, git-credential-manager) can't inject a conflicting
        # Authorization header alongside our extraHeader. GIT_CONFIG_NOSYSTEM
        # is the only reliable way to silence system-level helpers — setting
        # credential.helper="" via GIT_CONFIG_KEY_* only appends an empty
        # entry without clearing the system-installed one.
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = f"http.https://{host}/.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {basic}"
    return env


async def _rev_parse(workspace: Path) -> str | None:
    """The cloned commit SHA, or None if it can't be read (best-effort)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(workspace),
            "rev-parse",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0:
            return out.decode(errors="replace").strip() or None
    except Exception:  # pragma: no cover - defensive
        pass
    return None


async def _git_clone(
    url: str, credential: Mapping[str, str] | None = None
) -> tuple[Path, str | None]:
    workspace = Path(tempfile.mkdtemp(prefix="computeragent-substrate-"))
    proc = await asyncio.create_subprocess_exec(
        "git",
        "clone",
        "--depth",
        "1",
        url,
        str(workspace),
        env=_clone_env(url, credential),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        shutil.rmtree(workspace, ignore_errors=True)
        # Log/raise with host only — the token is in env config, not the URL,
        # but keep error surfaces clean regardless.
        raise RuntimeError(
            f"git clone failed for host {_host_of(url)}: {stderr.decode(errors='replace')!r}"
        )
    revision = await _rev_parse(workspace)
    logger.debug("cloned host %s into %s (sha %s)", _host_of(url), workspace, (revision or "?")[:8])
    return workspace, revision


_LIVE_TEMP_WORKSPACES: set[str] = set()


def _reap_temp_workspaces() -> None:
    """Last-resort cleanup for a process that never called ``dispose()``.

    A caller who skips ``async with`` leaves the cloned workspace behind, and
    there is no finalizer that could safely remove it — ``__del__`` runs at an
    arbitrary point during async teardown. An atexit sweep is the honest net:
    it cannot help a killed process, but it handles the common case of simply
    forgetting.
    """
    for path in list(_LIVE_TEMP_WORKSPACES):
        shutil.rmtree(path, ignore_errors=True)
    _LIVE_TEMP_WORKSPACES.clear()


atexit.register(_reap_temp_workspaces)


def _make_rmtree_close(path: Path) -> Any:
    _LIVE_TEMP_WORKSPACES.add(str(path))

    async def _close() -> None:
        shutil.rmtree(path, ignore_errors=True)
        _LIVE_TEMP_WORKSPACES.discard(str(path))

    return _close


async def _noop_close() -> None:
    return None


__all__ = ["LocalSubstrate"]
