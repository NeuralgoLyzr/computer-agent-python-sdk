"""Safe path SEGMENTS — Python port of the segment half of the TS ``path-jail.ts``.

The TS module also carries ``resolveJailedPath``, which guards harness-server's
filesystem HTTP routes. This SDK exposes no such routes, so only the segment
rules port: they are what the orchestration layer needs, because an LLM-authored
worker id becomes both a directory name and a git branch name.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

#: Identifier-shaped inputs (session ids, worker ids) that become a SINGLE path
#: segment. Stricter than a path jail: no separators at all, so the value can
#: never name a different directory regardless of how it is joined.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

SAFE_PATH_SEGMENT_DESC = "must match [A-Za-z0-9._-]{1,128} and cannot be '.' or '..'"
"""The rule, as one string. Shared so every rejection words it identically."""


class PathEscapeError(ValueError):
    """A value that would have escaped its intended single path segment."""

    def __init__(self, message: str, attempted: str) -> None:
        super().__init__(message)
        self.attempted = attempted


def is_safe_path_segment(name: Any) -> bool:
    return (
        isinstance(name, str) and name not in (".", "..") and _SAFE_SEGMENT.match(name) is not None
    )


def assert_safe_path_segment(name: Any, what: str = "identifier") -> str:
    if not is_safe_path_segment(name):
        raise PathEscapeError(f"{what} {SAFE_PATH_SEGMENT_DESC}", str(name))
    return str(name)


def to_safe_path_segment(name: Any) -> str | None:
    """Coerce an arbitrary label into a safe single path segment, or ``None`` if
    nothing usable survives.

    Used where the value is a LABEL rather than a lookup key — an LLM-authored
    worker id, say — so the caller can keep running instead of failing on a
    cosmetic difference. Never use this on a value that must round-trip (a
    session id identifies stored state; silently rewriting it would resume the
    wrong conversation).
    """
    if not isinstance(name, str):
        return None
    slug = unicodedata.normalize("NFKD", name)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", slug)
    slug = re.sub(r"^[-.]+", "", slug)
    slug = re.sub(r"[-.]+$", "", slug)
    slug = slug[:128]
    slug = re.sub(r"[-.]+$", "", slug)
    return slug if is_safe_path_segment(slug) else None


__all__ = [
    "SAFE_PATH_SEGMENT_DESC",
    "PathEscapeError",
    "assert_safe_path_segment",
    "is_safe_path_segment",
    "to_safe_path_segment",
]
