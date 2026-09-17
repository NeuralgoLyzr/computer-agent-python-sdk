"""Engine-shape knowledge for the options bags the HARNESS builds itself.

WHY THIS EXISTS. A worker session is the one kind of session nobody outside the
harness configures: the worktree dispatcher composes its options bag from the
roster and hands it to whatever engine the orchestrator runs. That bag was
Claude-SDK-shaped, and gitclaw mis-consumes every field it happens to share a
name with — silently, because the field is present and the wrong type. The MCP
layer already keeps its own engine list for exactly this; this is that list for
the worker-options builder and the worker's permission wiring.
"""

from __future__ import annotations

#: Engine names backed by gitclaw.
_GITCLAW_SHAPED = frozenset({"gitagent"})


def is_gitclaw_shaped(engine_name: str) -> bool:
    """True for engines backed by gitclaw. Three consequences, none of which
    produces an error message on its own:

    1. OPTIONS SHAPE. gitclaw's ``system_prompt`` is a plain STRING that
       REPLACES the prompt its own loader stitched from the GAP repo (SOUL.md,
       RULES.md, skills, manifest); ``system_prompt_suffix`` is the field it
       APPENDS. Handing it the Claude preset object therefore threw the GAP
       prompt away and stringified the object, so the worker's role and scope
       instruction never reached the model at all.
    2. PERMISSIONS. gitclaw wires its pre-tool-use hook unconditionally and has
       no ``permission_mode: "bypassPermissions"`` equivalent (it ignores the
       field), so every tool call takes the harness's permission path — which
       for a worker means waiting forever on an answer nobody will send.
    3. NO IN-SESSION FAN-OUT. It ignores the dispatch hook, so
       ``mode: "supervised"`` would hand the orchestrator no ``dispatch_many``
       tool at all.
    """
    return engine_name in _GITCLAW_SHAPED


__all__ = ["is_gitclaw_shaped"]
