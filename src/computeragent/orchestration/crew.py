"""Crew entry → runnable worker spec, and ``use:`` → identity source."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .contract import WorkerCrewEntry
    from .spawn_child import WorkerSpec

_SCHEME = re.compile(r"^(https?|ssh|git)://", re.IGNORECASE)


def crew_entry_to_worker_spec(entry: WorkerCrewEntry, task: str) -> WorkerSpec:
    """Map a declarative crew entry (``agents/<name>.yaml`` with ``use:``) plus
    the orchestrator-supplied per-run ``task`` into a runnable ``WorkerSpec``.

    The crew entry carries the STATIC config (which repo, scope, budget, session
    mode); the orchestrator supplies the DYNAMIC task at dispatch time. ``scope``
    is carried through and becomes the worker's scope-guard at dispatch.
    """
    from .spawn_child import WorkerSpec

    options: dict[str, Any] = {}
    if entry.model:
        options["model"] = entry.model
    if entry.tool_subset:
        options["allowed_tools"] = list(entry.tool_subset)
    if entry.max_turns is not None:
        options["max_turns"] = entry.max_turns
    if entry.budget_usd is not None:
        options["max_budget_usd"] = entry.budget_usd

    return WorkerSpec(
        id=entry.name,
        source=use_to_source(entry.use),
        task=task,
        session_mode="separate" if entry.separate_session else "sharedId",
        scope=entry.scope,
        # Stays OFF the options bag: `options` is engine config, `mcp` is a
        # policy the spawn site resolves against the orchestration block's
        # default.
        mcp=entry.mcp,
        options=options or None,
    )


def use_to_source(use: str) -> Any:
    """Resolve a ``use:`` string into an identity source.

    ``/abs``, ``./rel``, ``../rel``, ``~/home``
        local path
    ``https://host/org/repo[@ref]``
        git (scheme kept)
    ``host/org/repo[@ref]``
        git (``https://`` added — the GAP convention)

    A bare registered-agent id (no path/host) is NOT resolvable here — that
    needs the AgentOS registry; such a value is treated as a git url as a
    best-effort and will simply fail to clone, surfaced as a worker error.

    NOTE on the return type. This SDK's ``IdentitySource`` is a ``Path`` | a git
    URL string | an inline manifest dict, where TS has a tagged union. A local
    path therefore comes back as a ``Path`` and a git source as the URL string —
    which is what ``LocalSubstrate`` consumes. A ``@ref`` has nowhere to go in
    that shape, so it is kept on the URL only when the URL already carried one;
    otherwise it is dropped and the clone takes the default branch. That is a
    real narrowing against TS and is why it is stated here rather than hidden.
    """
    trimmed = use.strip()

    if trimmed.startswith(("/", "./", "../", "~")):
        return Path(trimmed).expanduser()

    # Split an optional `@ref` — only when the `@` follows the host/org part (a
    # `/` precedes it), so a scp-style `git@host:...` prefix isn't mistaken for
    # one.
    #
    # DIVERGENCE FROM THE TS PORT, and it is a bug fix. TS searches the whole
    # string, so a scheme's own `//` counts as the preceding slash: for
    # `ssh://git@host/org/repo` it finds `/` at index 4, `@` at index 8, splits
    # there, and resolves the source to `ssh://git` with ref `host/org/repo` —
    # an address that cannot clone. Searching only AFTER the scheme keeps every
    # form the TS gets right (`https://host/org/repo@v2` still splits,
    # `git@host:org/repo` still doesn't) and stops mangling the one it doesn't.
    scheme = _SCHEME.match(trimmed)
    offset = scheme.end() if scheme else 0
    rest = trimmed[offset:]
    at = rest.rfind("@")
    slash = rest.find("/")
    url = trimmed[: offset + at] if at > 0 and 0 <= slash < at else trimmed

    # Add a scheme for bare `host/org/repo` forms.
    if not _SCHEME.match(url) and not url.startswith("git@"):
        url = f"https://{url}"

    return url


__all__ = ["crew_entry_to_worker_spec", "use_to_source"]
