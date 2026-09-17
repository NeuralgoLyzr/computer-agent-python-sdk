"""The minimatch subset the scope-guard matches worker scopes with.

``fnmatch`` is not a substitute — its ``*`` crosses ``/``, so ``src/*`` would
match ``src/a/b`` and every scope would be wider than it reads.
"""

from __future__ import annotations

import pytest

from computeragent.orchestration.globmatch import glob_match, matches_any


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        # `*` stops at a separator.
        ("src/a.py", "src/*", True),
        ("src/a/b.py", "src/*", False),
        # Trailing `**` is one or more segments — `a/**` does NOT match `a`,
        # which is minimatch's own behaviour and the one people trip on.
        ("src/a.py", "src/**", True),
        ("src/a/b/c.py", "src/**", True),
        ("src", "src/**", False),
        # Middle and leading `**` are zero or more.
        ("a/b", "a/**/b", True),
        ("a/x/y/b", "a/**/b", True),
        ("b", "**/b", True),
        ("x/y/b", "**/b", True),
        ("anything/at/all", "**", True),
        # `?`, classes, braces.
        ("ab", "a?", True),
        ("abc", "a?", False),
        ("a1", "a[0-9]", True),
        ("ax", "a[0-9]", False),
        ("ax", "a[!0-9]", True),
        ("src/a.ts", "src/*.{ts,js}", True),
        ("src/a.js", "src/*.{ts,js}", True),
        ("src/a.py", "src/*.{ts,js}", False),
        # dot: true — a leading dot is not special.
        (".env", "*", True),
        (".github/workflows/ci.yml", "**/*.yml", True),
        # Anchored at both ends: no accidental substring match.
        ("notsrc/a.py", "src/**", False),
        ("src/a.pyc", "src/*.py", False),
    ],
)
def test_matches_minimatch(path, pattern, expected):
    assert glob_match(path, pattern) is expected


def test_a_regex_metacharacter_in_a_pattern_is_a_literal():
    assert glob_match("a+b.txt", "a+b.txt") is True
    assert glob_match("axb.txt", "a+b.txt") is False


def test_an_unclosed_class_is_a_literal_bracket():
    assert glob_match("[abc", "[abc") is True


def test_matches_any_is_false_for_an_empty_or_absent_list():
    assert matches_any("a.py", ()) is False
    assert matches_any("a.py", None) is False
    assert matches_any("a.py", ["*.md", "*.py"]) is True
