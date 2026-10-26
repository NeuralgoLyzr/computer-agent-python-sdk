"""Workspace file routes, confined to the session's workspace.

Port of ``harness-server/src/services/workspace-fs.ts`` and ``path-jail.ts``:
a path is relative, has no null byte or ``..`` segment, and resolves (symlinks
included) inside the workspace. A path to create may not exist yet.

Deletion removes a file or an EMPTY directory only. ``recursive=true`` is
accepted on the wire but a non-empty directory is refused with
``WORKDIR_DIRECTORY_NOT_EMPTY``: this server never deletes a tree.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any


class FsError(Exception):
    """A file-route failure with its harness error code and HTTP status."""

    def __init__(self, code: str, status: int, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.status = status
        self.detail = detail


def _escape(detail: str) -> FsError:
    return FsError("PATH_ESCAPE_REJECTED", 400, detail)


def _inside(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def resolve_jailed(workspace: Path, rel_path: str, *, must_exist: bool = False) -> Path:
    """``rel_path`` inside ``workspace``, or :class:`FsError` (``PATH_ESCAPE_REJECTED``)."""
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise _escape("path must be a non-empty string")
    if "\0" in rel_path:
        raise _escape("path contains a null byte")
    if os.path.isabs(rel_path):
        raise _escape("absolute paths are not allowed")
    normalized = os.path.normpath(rel_path)
    if normalized == ".." or normalized.startswith(".." + os.sep):
        raise _escape("parent traversal is not allowed")
    root = workspace.resolve()
    candidate = root / normalized
    if not _inside(root, Path(os.path.abspath(candidate))):
        raise _escape("path resolves outside the workspace")
    if candidate.exists() or candidate.is_symlink():
        real = candidate.resolve()
        if not _inside(root, real):
            raise _escape("symlink target is outside the workspace")
        return real
    if must_exist:
        raise FsError("WORKDIR_FILE_NOT_FOUND", 404, "no such file or directory")
    return candidate


def os_error(err: OSError) -> FsError:
    """An OS error as the harness's ``WORKDIR_*`` error."""
    if isinstance(err, FileNotFoundError):
        return FsError("WORKDIR_FILE_NOT_FOUND", 404, "no such file or directory")
    if isinstance(err, IsADirectoryError):
        return FsError("WORKDIR_PATH_IS_DIRECTORY", 400, "the path is a directory")
    if isinstance(err, FileExistsError):
        return FsError("WORKDIR_PATH_EXISTS", 409, "the path already exists")
    if isinstance(err, PermissionError):
        return FsError("WORKDIR_PERMISSION_DENIED", 403, "permission denied")
    if err.errno == errno.ENOTEMPTY:
        return FsError("WORKDIR_DIRECTORY_NOT_EMPTY", 409, "the directory is not empty")
    raise err


def list_tree(workspace: Path, rel_path: str, depth: int) -> list[dict[str, Any]]:
    root = resolve_jailed(workspace, rel_path or ".", must_exist=True)
    out: list[dict[str, Any]] = []

    def collect(directory: Path, remaining: int) -> None:
        for entry in sorted(directory.iterdir(), key=lambda p: p.name):
            st = entry.stat()
            is_dir = entry.is_dir()
            out.append(
                {
                    "path": str(entry.relative_to(root)),
                    "type": "dir" if is_dir else "file",
                    "size": st.st_size,
                    "mtime": int(st.st_mtime * 1000),
                    "mode": st.st_mode,
                }
            )
            if is_dir and remaining > 0:
                collect(entry, remaining - 1)

    if not root.is_dir():
        raise FsError("WORKDIR_PATH_IS_DIRECTORY", 400, "the path is not a directory")
    collect(root, max(0, depth))
    return out


def read_bytes(workspace: Path, rel_path: str) -> bytes:
    return resolve_jailed(workspace, rel_path, must_exist=True).read_bytes()


def write_bytes(workspace: Path, rel_path: str, data: bytes) -> int:
    target = resolve_jailed(workspace, rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target.stat().st_size


def edit_file(workspace: Path, rel_path: str, old: str, new: str, replace_all: bool) -> int:
    target = resolve_jailed(workspace, rel_path, must_exist=True)
    original = target.read_text(encoding="utf-8")
    count = original.count(old) if old else 0
    if count == 0:
        raise FsError("WORKDIR_EDIT_TARGET_NOT_FOUND", 422, "the text to replace was not found")
    if replace_all:
        updated, replacements = original.replace(old, new), count
    else:
        updated, replacements = original.replace(old, new, 1), 1
    target.write_text(updated, encoding="utf-8")
    return replacements


def remove_path(workspace: Path, rel_path: str) -> None:
    """Remove a file or an empty directory; a non-empty one is refused."""
    target = resolve_jailed(workspace, rel_path, must_exist=True)
    if target == workspace.resolve():
        raise _escape("the workspace itself cannot be removed")
    if target.is_dir() and not target.is_symlink():
        target.rmdir()
    else:
        target.unlink()


def make_dir(workspace: Path, rel_path: str, recursive: bool) -> None:
    target = resolve_jailed(workspace, rel_path)
    target.mkdir(parents=recursive, exist_ok=recursive)


def move_path(workspace: Path, source: str, dest: str) -> None:
    src = resolve_jailed(workspace, source, must_exist=True)
    dst = resolve_jailed(workspace, dest)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)


__all__ = [
    "FsError",
    "edit_file",
    "list_tree",
    "make_dir",
    "move_path",
    "os_error",
    "read_bytes",
    "remove_path",
    "resolve_jailed",
    "write_bytes",
]
