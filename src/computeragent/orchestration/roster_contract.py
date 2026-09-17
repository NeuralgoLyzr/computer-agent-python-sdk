"""The roster contract, appended by the HARNESS to every ``mode: "roster"``
orchestrator's system prompt.

WHY the harness owns this rather than the agent repo: the rule is a property of
the protocol the harness PARSES, not of any one agent's personality. Every
orchestrator prompt in the wild says some version of "decompose the task and
emit exactly one JSON block, nothing else" — and none of them say what to do
when the task needs no decomposition. Asked a plain question, the model obeys
the only rule it was given and emits ``{"workers":[]}``: nobody is dispatched,
and the user gets machine output where an answer should be. Shipping the missing
clause here fixes every existing agent at once, with no repo to update and no
release to coordinate.

This is a belt-and-braces layer, NOT the guarantee. :mod:`.parse_roster` and the
roster runner already make an empty roster unshowable — the harness withholds it
and spends a turn getting a real answer. This just stops the wasted turn
happening in the first place, and it is a prompt, so it persuades rather than
enforces.
"""

from __future__ import annotations

from typing import Any

from .engine_shape import is_gitclaw_shaped

ROSTER_CONTRACT = "\n".join(
    [
        "## Orchestration contract (harness-enforced)",
        "",
        "This overrides any instruction above that conflicts with it.",
        "",
        "- Needs worker sub-agents? Emit the roster JSON block and nothing else, "
        "exactly as described above.",
        "- Does NOT need them — a question, a lookup, a one-step task, anything "
        "you can finish yourself?",
        "  Answer the user directly in plain prose. No JSON, no roster, no plan.",
        "- NEVER emit a roster with an empty worker list. It dispatches nobody and answers nobody;",
        "  the harness discards it and has to ask you again, which costs the user a round trip.",
    ]
)


def append_roster_contract(options: Any, engine_name: str) -> Any:
    """Append :data:`ROSTER_CONTRACT` to whatever system-prompt field this engine
    actually appends with.

    The two engine shapes are NOT interchangeable, and getting it wrong destroys
    the agent: the Claude SDK takes an OBJECT whose ``append`` holds the
    GAP-stitched prompt, while gitclaw's ``system_prompt`` is a plain string
    that REPLACES its loader's prompt.

    DIVERGENCE FROM THE TS PORT, deliberate. The TS writes gitclaw's dedicated
    ``systemPromptSuffix`` field. This SDK's ``GitAgentEngine`` has no such
    field — it reads ``system_prompt`` and nothing else — so writing a suffix
    key here would be silently dropped and the contract would never reach the
    model. Concatenating onto ``system_prompt`` is the only append gitclaw has
    in Python, and it preserves the loader's stitched prompt, which is the
    property the TS field exists to protect.

    Returns ``options`` untouched when it isn't a plain dict, so a loader that
    produced nothing can never be turned INTO something here.
    """
    if not isinstance(options, dict):
        return options

    if is_gitclaw_shaped(engine_name):
        existing = options.get("system_prompt")
        existing_str = existing if isinstance(existing, str) else ""
        return {
            **options,
            "system_prompt": (
                f"{existing_str}\n\n{ROSTER_CONTRACT}" if existing_str else ROSTER_CONTRACT
            ),
        }

    sp = options.get("system_prompt")
    # The loader's shape: {"type": "preset", "preset": "claude_code", "append": …}.
    # Extend `append` in place so the GAP repo's SOUL/RULES text survives.
    if isinstance(sp, dict):
        existing = sp.get("append")
        existing_str = existing if isinstance(existing, str) else ""
        return {
            **options,
            "system_prompt": {
                **sp,
                "append": (
                    f"{existing_str}\n\n{ROSTER_CONTRACT}" if existing_str else ROSTER_CONTRACT
                ),
            },
        }
    # A bare string prompt (a caller-supplied override, or an engine that takes
    # one): concatenating is the only append available.
    if isinstance(sp, str):
        return {**options, "system_prompt": f"{sp}\n\n{ROSTER_CONTRACT}"}
    # No system prompt at all — introduce the preset shape the Claude engine
    # expects rather than inventing a bare string it would ignore.
    return {
        **options,
        "system_prompt": {
            "type": "preset",
            "preset": "claude_code",
            "append": ROSTER_CONTRACT,
        },
    }


__all__ = ["ROSTER_CONTRACT", "append_roster_contract"]
