"""Per-worker scope-guard — a policy decider that denies writes outside a
worker's declared ``scope.write``.

Enforced even under ``bypassPermissions``, because this SDK's Claude engine
routes the decider through a ``PreToolUse`` hook in exactly that case, and its
denials show up in the trace like any other policy decision.

Only WRITE tools are guarded (their target path is in the args). A shell tool is
blind (``Bash``, gitclaw's ``cli`` — a command string doesn't say what it
touches), so it is allowed and measured post-hoc against the worktree diff. Same
for a write whose target isn't an argument at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .globmatch import matches_any

#: Write-tool names, LOWERCASED for comparison. ``Write``/``Edit``/``MultiEdit``/
#: ``NotebookEdit`` are the Claude-SDK names; ``write``/``edit`` are gitclaw's
#: own. The match must be case-insensitive: an exact-case check returned ALLOW
#: for EVERY gitagent write, so a scoped worker could write anywhere in the repo
#: and only the post-hoc diff noticed.
_WRITE_TOOLS = frozenset({"write", "edit", "multiedit", "notebookedit"})

#: gitclaw's write/edit tools take ``path``; the Claude SDK's take ``file_path``.
_PATH_FIELDS = ("file_path", "notebook_path", "path", "filePath")


def _allow(tool_input: Any) -> Any:
    from claude_agent_sdk.types import PermissionResultAllow

    return PermissionResultAllow(updated_input=tool_input)


def _deny(reason: str) -> Any:
    from claude_agent_sdk.types import PermissionResultDeny

    return PermissionResultDeny(message=reason, interrupt=False)


def _target_path(args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    for field in _PATH_FIELDS:
        value = args.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _relative_to_workspace(raw: str, workspace: Path) -> str | None:
    """The path of ``raw`` relative to ``workspace``, forward-slashed, or
    ``None`` when it escapes the workspace. A relative ``raw`` is resolved
    against the workspace."""
    root = Path(workspace).resolve()
    candidate = Path(raw)
    absolute = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        rel = absolute.relative_to(root)
    except ValueError:
        return None
    return "" if str(rel) == "." else rel.as_posix()


class ScopeGuardDecider:
    """Denies writes outside ``scope_write``.

    :param scope_write: globs (workspace-relative) this worker may write. Empty
        ⇒ no writes allowed.
    :param workspace: the worker's workspace root; write targets are resolved
        against it.
    :param always_allow: globs always allowed regardless of scope.
    """

    def __init__(
        self,
        *,
        scope_write: tuple[str, ...],
        workspace: Path,
        always_allow: tuple[str, ...] = (),
    ) -> None:
        self.scope_write = tuple(scope_write)
        self.workspace = Path(workspace)
        self.always_allow = tuple(always_allow)

    async def __call__(self, tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        del context
        if tool_name.lower() not in _WRITE_TOOLS:
            return _allow(tool_input)

        raw = _target_path(tool_input)
        # A write tool with no recognizable target: allow it and let the
        # worktree diff settle what it did. Guessing would be worse than
        # measuring.
        if raw is None:
            return _allow(tool_input)

        rel = _relative_to_workspace(raw, self.workspace)
        if rel is None:
            return _deny(
                f"{raw} is outside this worker's workspace. "
                "Write only inside the repository you were given."
            )
        if matches_any(rel, self.always_allow):
            return _allow(tool_input)
        if not self.scope_write:
            return _deny("this worker declared no writable scope and may not write any file.")
        if not matches_any(rel, self.scope_write):
            return _deny(
                f"{rel} is outside your declared scope.write "
                f"({', '.join(self.scope_write)}). "
                "Another worker owns that surface — stay inside your scope."
            )
        return _allow(tool_input)


class AllowAllDecider:
    """The counterpart for a worker that declared NO scope: a decider that
    allows everything, so the worker never blocks waiting for a permission
    answer.

    WHY A DECIDER AT ALL. An engine that always routes tool calls through the
    permission path (gitclaw — no ``bypassPermissions``) has nobody subscribed
    to a worker's stream, so nothing can ever answer, and its first tool call
    hangs and takes the whole wave with it.

    WHY NOT a :class:`ScopeGuardDecider` with an empty scope: an empty
    ``scope_write`` DENIES every write — the worker would run but be unable to
    do its job. An unscoped worker is unscoped, and its writes are still bounded
    by its own git worktree and attributed by the post-hoc diff.
    """

    async def __call__(self, tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        del tool_name, context
        return _allow(tool_input)


__all__ = ["AllowAllDecider", "ScopeGuardDecider"]
