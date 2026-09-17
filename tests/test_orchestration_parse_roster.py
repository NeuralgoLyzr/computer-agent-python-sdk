"""The six roster outcomes, and the id normalisation that keeps an LLM-authored
label usable as a directory and a branch name."""

from __future__ import annotations

import pytest

from computeragent.orchestration.parse_roster import (
    is_roster_text,
    parse_roster,
    parse_roster_result,
)


def fenced(body: str) -> str:
    return f"Here is the plan.\n\n```json\n{body}\n```\n"


# ── outcomes ───────────────────────────────────────────────────────────────


def test_entries_outcome_for_a_usable_roster():
    parsed = parse_roster_result(fenced('{"workers": [{"id": "w1", "task": "do a thing"}]}'))
    assert parsed.outcome == "entries"
    assert [e.id for e in parsed.entries] == ["w1"]
    assert parsed.entries[0].task == "do a thing"


def test_roster_key_is_accepted_as_well_as_workers():
    parsed = parse_roster_result(fenced('{"roster": [{"task": "t"}]}'))
    assert parsed.outcome == "entries"


def test_an_empty_worker_list_is_a_deliberate_decline_not_a_failure():
    parsed = parse_roster_result(fenced('{"workers": []}'))
    assert parsed.outcome == "empty-roster"
    assert parsed.entries == ()


def test_items_without_a_task_are_no_usable_entries():
    parsed = parse_roster_result(fenced('{"workers": [{"id": "w1"}, {"task": ""}]}'))
    assert parsed.outcome == "no-usable-entries"


def test_unparseable_json_is_its_own_outcome():
    parsed = parse_roster_result(fenced('{"workers": [{"task": "un"escaped"}]}'))
    assert parsed.outcome == "invalid-json"


def test_json_without_a_roster_key_counts_as_prose():
    # Far more likely an illustrative fence inside an ordinary answer than a
    # roster, so it must not be treated as machine output.
    parsed = parse_roster_result(fenced('{"example": {"a": 1}}'))
    assert parsed.outcome == "not-a-roster"


def test_plain_prose_has_no_block():
    parsed = parse_roster_result("The answer is 42, because the question was silly.")
    assert parsed.outcome == "no-block"


def test_a_bare_object_is_found_without_a_fence():
    parsed = parse_roster_result('Sure:\n{"workers": [{"task": "t"}]}\nthanks')
    assert parsed.outcome == "entries"


# ── is_roster_text: the predicate the run flow branches on ─────────────────


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("entries", True),
        ("empty-roster", True),
        ("no-usable-entries", True),
        ("invalid-json", True),
        ("not-a-roster", False),
        ("no-block", False),
    ],
)
def test_is_roster_text_splits_machinery_from_prose(outcome, expected):
    assert is_roster_text(outcome) is expected


# ── the greedy fence ───────────────────────────────────────────────────────


def test_a_task_containing_a_code_fence_does_not_truncate_the_json():
    # A non-greedy match would stop at the inner fence and leave broken JSON.
    body = '{"workers": [{"task": "run ```sh\\nls\\n``` and report"}]}'
    parsed = parse_roster_result(fenced(body))
    assert parsed.outcome == "entries"
    assert "ls" in parsed.entries[0].task


# ── id normalisation ───────────────────────────────────────────────────────


def test_a_prose_id_is_slugified_rather_than_failing_the_worker():
    parsed = parse_roster_result(fenced('{"workers": [{"id": "backend api", "task": "t"}]}'))
    assert parsed.entries[0].id == "backend-api"


def test_an_id_with_a_slash_cannot_become_a_nested_path():
    parsed = parse_roster_result(fenced('{"workers": [{"id": "fix/auth", "task": "t"}]}'))
    assert parsed.entries[0].id == "fix-auth"


def test_an_unusable_id_is_dropped_so_the_positional_fallback_names_the_worker():
    parsed = parse_roster_result(fenced('{"workers": [{"id": "***", "task": "t"}]}'))
    assert parsed.entries[0].id is None


def test_two_prose_ids_that_slugify_alike_do_not_collapse_onto_one_worktree():
    parsed = parse_roster_result(
        fenced('{"workers": [{"id": "back end", "task": "a"}, {"id": "back-end", "task": "b"}]}')
    )
    assert [e.id for e in parsed.entries] == ["back-end", "back-end-2"]


def test_an_explicit_id_is_never_renamed_onto_a_positional_fallback():
    # Worker 1 has no id and will be named `w2` by position, so worker 0's
    # explicit `w2` must move instead of silently colliding.
    parsed = parse_roster_result(fenced('{"workers": [{"id": "w2", "task": "a"}, {"task": "b"}]}'))
    ids = [e.id for e in parsed.entries]
    assert ids[0] == "w2-2"
    assert ids[1] is None


def test_scope_and_tools_survive_parsing():
    parsed = parse_roster_result(
        fenced(
            '{"workers": [{"task": "t", "role": "api", "model": "m", '
            '"tools": ["Read", 7], "scope": {"write": ["src/**"], "read": 3}}]}'
        )
    )
    entry = parsed.entries[0]
    assert entry.role == "api"
    assert entry.model == "m"
    assert entry.tools == ("Read",)
    assert entry.scope is not None
    assert entry.scope.write == ("src/**",)
    assert entry.scope.read is None


def test_parse_roster_is_the_entries_only_view():
    assert parse_roster(fenced('{"workers": []}')) == ()
    assert len(parse_roster(fenced('{"workers": [{"task": "t"}]}'))) == 1
