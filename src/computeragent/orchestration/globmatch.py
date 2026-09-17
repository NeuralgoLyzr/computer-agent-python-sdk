"""A minimatch-compatible subset, translated to ``re``.

The TS scope-guard matches worker scopes with ``minimatch``. Python's ``fnmatch``
is not a substitute: it has no ``**``, its ``*`` crosses ``/``, and its ``?``
matches a separator — so ``src/*`` would match ``src/a/b`` and a scope would be
wider than it reads. ``pathlib.PurePath.match`` is closer but anchors at the
right-hand side, which silently widens scopes the other way.

So this is a direct translation of the minimatch features a scope glob actually
uses, with the ``dot: true`` semantics the guard passes (``*`` matches a leading
dot, which falls out naturally here):

* ``{a,b}``  brace alternation, nestable, expanded before translation
* ``**``     zero or more path segments in the middle, one or more at the end
             (``a/**`` matches ``a/b`` and ``a/b/c``, NOT ``a`` — minimatch's
             own behaviour, and the one people are most often surprised by)
* ``*``      any run of non-separator characters
* ``?``      exactly one non-separator character
* ``[abc]`` / ``[!abc]`` / ``[a-z]``  character classes

Deliberately NOT supported (they do not appear in a scope and guessing would be
worse than refusing): extglob (``+(a|b)``), ``!`` pattern negation, and comments.
A pattern using them is translated literally, so it simply matches nothing
rather than matching too much.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

_BRACE = re.compile(r"\{([^{}]*)\}")


def _expand_braces(pattern: str) -> list[str]:
    """Expand ``{a,b}`` alternations, innermost first, into concrete patterns."""
    match = _BRACE.search(pattern)
    if match is None:
        return [pattern]
    head, tail = pattern[: match.start()], pattern[match.end() :]
    out: list[str] = []
    for alt in match.group(1).split(","):
        out.extend(_expand_braces(f"{head}{alt}{tail}"))
    return out


def _segment_regex(segment: str) -> str:
    """Translate one path segment (no ``/``) to a regex fragment."""
    out: list[str] = []
    i = 0
    n = len(segment)
    while i < n:
        ch = segment[i]
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            close = segment.find("]", i + 2)
            if close < 0:
                # An unclosed class is a literal '[' in minimatch too.
                out.append(re.escape(ch))
            else:
                body = segment[i + 1 : close]
                if body.startswith(("!", "^")):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = close + 1
                continue
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


def _pattern_regex(pattern: str) -> str:
    parts = pattern.split("/")
    last = len(parts) - 1
    out: list[str] = []
    for i, seg in enumerate(parts):
        first = i == 0
        if seg == "**":
            if i == last:
                # Trailing globstar: one or more remaining segments. `a/**` does
                # not match `a` — matching minimatch, where the pattern still
                # has a part left when the file is exhausted.
                out.append(("" if first else "/") + ".+")
            else:
                # Middle/leading globstar: zero or more segments, separator
                # included, so the following segment emits none of its own.
                out.append(("" if first else "/") + "(?:[^/]+/)*")
        else:
            needs_sep = not first and not out[-1].endswith("/)*")
            out.append(("/" if needs_sep else "") + _segment_regex(seg))
    return "".join(out)


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(f"^{_pattern_regex(p)}$") for p in _expand_braces(pattern))


def glob_match(path: str, pattern: str) -> bool:
    """Does ``path`` (forward-slashed, workspace-relative) match ``pattern``?"""
    return any(rx.match(path) for rx in _compiled(pattern))


def matches_any(path: str, patterns: Iterable[str] | None) -> bool:
    if not patterns:
        return False
    return any(glob_match(path, p) for p in patterns)


__all__ = ["glob_match", "matches_any"]
