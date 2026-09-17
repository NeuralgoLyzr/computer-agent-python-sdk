"""Extract a worker roster from an orchestrator's turn text."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Any, Literal

from .contract import WorkerScope
from .path_jail import is_safe_path_segment, to_safe_path_segment

logger = logging.getLogger("computeragent.orchestration.roster")


@dataclass(frozen=True)
class RosterEntry:
    """One instance in an orchestrator's composed roster.

    Two shapes, by orchestration mode:

    * independent (``use:``): ``use`` names a crew entry (an ``agents/<name>``
      template) and ``id`` is the per-instance id.
    * shared-codebase (worktree): the worker is defined INLINE — ``role`` (its
      system prompt), ``scope`` (writable globs), and optional ``model``/
      ``tools`` — and edits the shared feature repo in its own worktree.

    ``task`` is always the per-instance work.
    """

    task: str
    id: str | None = None
    use: str | None = None
    role: str | None = None
    scope: WorkerScope | None = None
    model: str | None = None
    tools: tuple[str, ...] | None = None


RosterOutcome = Literal[
    "entries",
    "empty-roster",
    "no-usable-entries",
    "invalid-json",
    "not-a-roster",
    "no-block",
]
"""What the orchestrator's turn text turned out to BE.

A parse used to answer only "which workers?", returning ``[]`` for five
different situations at once — which left the run flow unable to tell "the agent
answered in prose" from "the agent emitted a roster with nobody in it". That
ambiguity is what put a raw ``{"workers":[]}`` in front of a user: with no way
to know the text was machine output, the run flow left it standing as the
answer.

The distinction every caller actually needs is ROSTER TEXT vs PROSE:

* roster text is machinery and must never reach the user — the run flow
  withholds it and spends one more turn getting a real answer;
* prose IS the answer and must be left exactly as written.

``entries``
    ≥1 usable worker — dispatch.
``empty-roster``
    a ``workers``/``roster`` key holding an EMPTY array: the agent explicitly
    declined to fan out. NOT an error — a roster agent is never obliged to
    delegate.
``no-usable-entries``
    that array had items but none carried a usable ``task`` — a broken roster,
    and a real gate failure.
``invalid-json``
    a block was found but parsing threw — ditto.
``not-a-roster``
    valid JSON carrying no ``workers``/``roster`` key at all. Ambiguous, and far
    more likely an example fence inside a prose answer than a roster, so it
    counts AS prose.
``no-block``
    no JSON block at all — prose.
"""


@dataclass(frozen=True)
class RosterParse:
    outcome: RosterOutcome
    entries: tuple[RosterEntry, ...] = ()


def is_roster_text(outcome: RosterOutcome) -> bool:
    """Is this text the orchestrator's machinery rather than its answer?

    THE predicate the run flow branches on. Anything not demonstrably a roster
    counts as prose, so an agent that answers normally keeps its answer intact —
    the failure being fixed (JSON on the user's screen, no answer at all) is
    strictly worse than the one this risks (one extra synthesis turn).
    """
    return outcome in ("entries", "empty-roster", "no-usable-entries", "invalid-json")


# The fence match is GREEDY to the LAST ``` — a worker `task` routinely embeds
# ```code fences, and a non-greedy match would truncate the JSON at the first
# inner fence.
_FENCED = re.compile(r"```json\s*(.*)```", re.DOTALL)
_BARE = re.compile(r"(\{.*\"(?:workers|roster)\".*\})", re.DOTALL)


def _extract_json_block(text: str) -> str | None:
    fenced = _FENCED.search(text)
    if fenced:
        return fenced.group(1).strip()
    bare = _BARE.search(text)
    return bare.group(1).strip() if bare else None


def _has_roster_key(obj: Any) -> bool:
    """Does this object carry a roster key at all (empty array included)?"""
    if not isinstance(obj, dict):
        return False
    return isinstance(obj.get("workers"), list) or isinstance(obj.get("roster"), list)


def _pick_array(obj: Any) -> list[Any]:
    if not isinstance(obj, dict):
        return []
    for key in ("workers", "roster"):
        value = obj.get(key)
        if isinstance(value, list):
            return list(value)
    return []


def _scope_of(raw: Any) -> WorkerScope | None:
    if not isinstance(raw, dict):
        return None
    write = (
        tuple(x for x in raw["write"] if isinstance(x, str))
        if isinstance(raw.get("write"), list)
        else None
    )
    read = (
        tuple(x for x in raw["read"] if isinstance(x, str))
        if isinstance(raw.get("read"), list)
        else None
    )
    if write is None and read is None:
        return None
    return WorkerScope(write=write, read=read)


def parse_roster_result(text: str) -> RosterParse:
    """Extract a roster from the orchestrator's output text. Accepts a ``json``
    fenced block or a bare object, keyed ``workers`` or ``roster``.

    The ``outcome`` tells the CALLER which zero-worker case it is; the warnings
    tell the OPERATOR, with the detail needed to act. Observed for real: a model
    wrote a ``task`` string containing literal newlines and unescaped quotes,
    parsing threw, the wave dispatched nothing, and the run still reported
    complete with a passing gate.
    """
    raw = _extract_json_block(text)
    if raw is None:
        logger.warning(
            "roster.no_json_block: %d chars, tail=%r",
            len(text),
            # The tail is where a fenced block would be, and it's what the
            # operator needs to see to know whether the model answered in prose.
            text[-400:],
        )
        return RosterParse(outcome="no-block")

    try:
        obj = json.loads(raw)
    except ValueError as exc:
        logger.warning("roster.invalid_json: %s (rawLen=%d) raw=%r", exc, len(raw), raw[:800])
        return RosterParse(outcome="invalid-json")

    keys = sorted(obj.keys()) if isinstance(obj, dict) else []
    # No `workers`/`roster` key at all. `_pick_array` cannot distinguish this
    # from an empty one, but the caller must: an empty ARRAY is a deliberate "no
    # workers needed", whereas a JSON block with neither key is most likely an
    # illustrative fence inside an ordinary answer.
    if not _has_roster_key(obj):
        logger.warning("roster.not_a_roster: keys=%s raw=%r", keys, raw[:800])
        return RosterParse(outcome="not-a-roster")

    arr = _pick_array(obj)
    if len(arr) == 0:
        # The agent emitted the roster shape and put nobody in it: it decided
        # the task needed no fan-out. Deliberate, and NOT a failure — but the
        # text is still machinery, so the caller must not surface it.
        logger.info("roster.empty: keys=%s raw=%r", keys, raw[:400])
        return RosterParse(outcome="empty-roster")

    out: list[RosterEntry] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        task = item.get("task")
        if not isinstance(task, str) or not task:
            continue
        tools = (
            tuple(t for t in item["tools"] if isinstance(t, str))
            if isinstance(item.get("tools"), list)
            else None
        )
        out.append(
            RosterEntry(
                task=task,
                id=item["id"] if isinstance(item.get("id"), str) else None,
                use=item["use"] if isinstance(item.get("use"), str) else None,
                role=item["role"] if isinstance(item.get("role"), str) else None,
                model=item["model"] if isinstance(item.get("model"), str) else None,
                tools=tools,
                scope=_scope_of(item.get("scope")),
            )
        )

    if not out:
        logger.warning(
            # Entries were present but none usable — almost always a missing or
            # empty `task`.
            "roster.no_entries: %d item(s), keys=%s raw=%r",
            len(arr),
            keys,
            raw[:800],
        )
        return RosterParse(outcome="no-usable-entries")

    return RosterParse(outcome="entries", entries=_normalize_ids(out))


def parse_roster(text: str) -> tuple[RosterEntry, ...]:
    """Entries-only form. Prefer :func:`parse_roster_result` anywhere the
    zero-worker cases have to be told apart."""
    return parse_roster_result(text).entries


def _normalize_ids(entries: list[RosterEntry]) -> tuple[RosterEntry, ...]:
    """A roster ``id`` is LLM-authored and becomes BOTH a worktree directory
    name and a git branch name, so it has to survive as a single safe path
    segment. The model has no idea about that constraint and routinely writes
    prose ids ("backend api", "fix/auth"), which the worktree layer rejects —
    failing that worker while the rest of the wave runs on.

    An id is a LABEL, not a lookup key (``use`` names the crew entry; nothing
    joins on ``id``), so the right move is to normalise it rather than fail:
    slugify, drop it entirely if nothing usable survives (the caller then falls
    back to ``w<n>``), and de-duplicate so two prose ids can't collapse onto one
    worktree. Every rewrite is logged — a silently renamed worker is hard to
    trace back to the roster the model actually wrote.
    """
    # Reserve the positional fallback (`w<n>`) for every entry that will
    # actually use one, so an explicit id can never be renamed onto a name
    # another worker is already going to take.
    taken: set[str] = {f"w{i + 1}" for i, e in enumerate(entries) if e.id is None}

    out: list[RosterEntry] = []
    for i, entry in enumerate(entries):
        original = entry.id
        if original is None:
            out.append(entry)
            continue

        slug = original if is_safe_path_segment(original) else to_safe_path_segment(original)
        if slug is None:
            # Nothing survived (e.g. "***"). Drop the id so the dispatcher's
            # positional `w<n>` fallback names the worker instead.
            logger.warning("roster.id_unusable: index=%d id=%r", i, original)
            out.append(replace(entry, id=None))
            continue
        if slug in taken:
            # 128-char cap: trim before appending so the suffix can't push it over.
            base = slug[:120]
            n = 2
            while f"{base}-{n}" in taken:
                n += 1
            slug = f"{base}-{n}"
            logger.warning("roster.id_collision: index=%d id=%r resolved=%r", i, original, slug)
        elif slug != original:
            logger.warning("roster.id_normalized: index=%d id=%r resolved=%r", i, original, slug)
        taken.add(slug)
        out.append(replace(entry, id=slug))
    return tuple(out)


__all__ = [
    "RosterEntry",
    "RosterOutcome",
    "RosterParse",
    "is_roster_text",
    "parse_roster",
    "parse_roster_result",
]
