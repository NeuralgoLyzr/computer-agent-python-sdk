"""The per-worker scope-guard, and the composite that stops a bound guardrail
being escaped by fanning out."""

from __future__ import annotations

from pathlib import Path

import pytest

from computeragent.orchestration.composite_decider import (
    CompositePolicyDecider,
    compose_deciders,
)
from computeragent.orchestration.scope_guard import AllowAllDecider, ScopeGuardDecider


def behavior(result) -> str:
    return getattr(result, "behavior", None) or result.get("behavior")


def message(result) -> str:
    return getattr(result, "message", "") or ""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def guard(workspace: Path) -> ScopeGuardDecider:
    return ScopeGuardDecider(scope_write=("src/**", "docs/*.md"), workspace=workspace)


# ── what is guarded ────────────────────────────────────────────────────────


async def test_a_non_write_tool_is_allowed_without_looking_at_paths(guard):
    assert behavior(await guard("Bash", {"command": "rm -rf /"}, None)) == "allow"


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
async def test_every_claude_write_tool_is_guarded(guard, tool):
    assert behavior(await guard(tool, {"file_path": "other/x.py"}, None)) == "deny"


@pytest.mark.parametrize("tool", ["write", "edit"])
async def test_gitclaw_lowercase_write_tools_are_guarded_too(guard, tool):
    # The match used to be exact-case, so every gitagent write returned ALLOW
    # and a scoped worker could write anywhere in the repo.
    assert behavior(await guard(tool, {"path": "other/x.py"}, None)) == "deny"


async def test_a_write_with_no_recognisable_target_is_allowed_and_measured_later(guard):
    assert behavior(await guard("Write", {"content": "hi"}, None)) == "allow"


# ── the scope itself ───────────────────────────────────────────────────────


async def test_a_write_inside_scope_is_allowed(guard):
    assert behavior(await guard("Write", {"file_path": "src/deep/mod.py"}, None)) == "allow"


async def test_a_write_outside_scope_names_the_scope_it_missed(guard):
    result = await guard("Write", {"file_path": "other/x.py"}, None)
    assert behavior(result) == "deny"
    assert "src/**" in message(result)
    assert "Another worker owns that surface" in message(result)


async def test_an_absolute_path_inside_the_workspace_is_resolved_relatively(guard, workspace):
    target = workspace / "src" / "mod.py"
    assert behavior(await guard("Write", {"file_path": str(target)}, None)) == "allow"


async def test_a_path_escaping_the_workspace_is_denied_before_the_globs_matter(guard, workspace):
    result = await guard("Write", {"file_path": "../outside.py"}, None)
    assert behavior(result) == "deny"
    assert "outside this worker's workspace" in message(result)


async def test_an_absolute_path_elsewhere_on_disk_is_denied(guard):
    result = await guard("Write", {"file_path": "/etc/passwd"}, None)
    assert behavior(result) == "deny"
    assert "outside this worker's workspace" in message(result)


async def test_an_empty_scope_denies_every_write_with_its_own_reason(workspace):
    guard = ScopeGuardDecider(scope_write=(), workspace=workspace)
    result = await guard("Write", {"file_path": "src/a.py"}, None)
    assert behavior(result) == "deny"
    assert "declared no writable scope" in message(result)


async def test_always_allow_beats_the_scope(workspace):
    guard = ScopeGuardDecider(
        scope_write=("src/**",), workspace=workspace, always_allow=("REPORT.md",)
    )
    assert behavior(await guard("Write", {"file_path": "REPORT.md"}, None)) == "allow"


async def test_the_allow_all_decider_exists_so_an_unscoped_worker_never_hangs():
    # Not a ScopeGuardDecider with an empty scope: that DENIES every write, so
    # the worker would run and be unable to do its job.
    assert behavior(await AllowAllDecider()("Write", {"file_path": "x"}, None)) == "allow"


# ── the composite ──────────────────────────────────────────────────────────


async def test_compose_collapses_zero_and_one():
    assert compose_deciders(()) is None
    assert compose_deciders((None,)) is None
    only = AllowAllDecider()
    assert compose_deciders((only, None)) is only


async def test_the_first_denier_wins_and_its_message_survives(workspace):
    guard = ScopeGuardDecider(scope_write=("src/**",), workspace=workspace)
    composite = compose_deciders((guard, AllowAllDecider()))
    result = await composite("Write", {"file_path": "other/x.py"}, None)
    assert behavior(result) == "deny"
    # Rewriting the decision here would replace a scope explanation with a
    # generic one, and the model would have nothing to act on.
    assert "outside your declared scope.write" in message(result)


async def test_a_later_denier_is_still_reached(workspace):
    class Denier:
        async def __call__(self, *_a, **_k):
            from claude_agent_sdk.types import PermissionResultDeny

            return PermissionResultDeny(message="policy says no", interrupt=False)

    composite = compose_deciders((AllowAllDecider(), Denier()))
    result = await composite("Bash", {}, None)
    assert behavior(result) == "deny"
    assert message(result) == "policy says no"


async def test_the_cheapest_member_short_circuits_the_expensive_one(workspace):
    calls: list[str] = []

    class Expensive:
        async def __call__(self, *_a, **_k):
            calls.append("network")
            from claude_agent_sdk.types import PermissionResultAllow

            return PermissionResultAllow(updated_input={})

    guard = ScopeGuardDecider(scope_write=("src/**",), workspace=workspace)
    composite = compose_deciders((guard, Expensive()))
    await composite("Write", {"file_path": "other/x.py"}, None)
    assert calls == []


async def test_an_empty_composite_denies_rather_than_allowing_everything():
    result = await CompositePolicyDecider(())("Bash", {}, None)
    assert behavior(result) == "deny"
    assert "no policy decider configured" in message(result)
