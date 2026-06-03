"""Run a GAP-shaped agent through the ``gitagent`` engine, end-to-end.

What this script proves:

1. ``LocalSubstrate`` git-clones an arbitrary public GitAgentProtocol
   repo into a temp dir.
2. ``GapIdentityLoader`` reads ``agent.yaml`` (+ ``SOUL.md`` /
   ``RULES.md`` / ``AGENTS.md`` if present) and resolves an
   :class:`AgentManifest` shaped for the ``gitagent`` engine: model,
   system_prompt (stitched), max_turns, constraints.
3. ``GitAgentEngine`` dispatches on the model prefix:
   * ``anthropic:claude-…`` / bare ``claude-…`` → native Anthropic
     Messages API via ``ANTHROPIC_API_KEY``.
   * ``openai:gpt-…`` / bare ``gpt-…`` / unknown →
     OpenAI-compatible Chat Completions via
     ``GITCLAW_MODEL_BASE_URL`` + ``OPENAI_API_KEY``.
4. Three sequential ``.chat()`` calls exercise the prior-turns context
   restoration (system-prompt suffix grows with each turn).

The lyzr-docs-agent default repo declares
``model: anthropic:claude-sonnet-4-5-20250929`` so this run hits
Anthropic directly with the key from your environment.

Caveat: 0.2.0 ``gitagent`` is MCP-only for tools. The cloned repo's
``tools/*.yaml`` definitions are surfaced as a ``tools_hint`` on the
manifest, but the engine's permission-request → tool-execution loop
returns a stub response — wire an MCP server for real filesystem tools.
Built-in tool runners land in 0.3.0.

Required env (Anthropic path, default for lyzr-docs-agent):

* ``ANTHROPIC_API_KEY``

Optional:

* ``ANTHROPIC_BASE_URL`` — for proxies / regional endpoints.
* ``GAP_REPO_URL`` — override the source repo (defaults to lyzr-docs-agent).

If you point ``GAP_REPO_URL`` at a repo whose ``agent.yaml`` lists an
OpenAI-shaped model, set ``GITCLAW_MODEL_BASE_URL`` + ``OPENAI_API_KEY``
instead and the engine will route to the OpenAI backend automatically.
"""

from __future__ import annotations

import asyncio
import os

from computeragent import ComputerAgent

_DEFAULT_REPO = "https://github.com/NeuralgoLyzr/lyzr-docs-agent"
_TURNS = [
    "In one sentence, what is the purpose of this agent?",
    "Which Lyzr product areas can you answer questions about?",
    "Summarise your behavioural rules in three short bullets.",
]


def _check_env() -> None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    if os.environ.get("GITCLAW_MODEL_BASE_URL") and os.environ.get("OPENAI_API_KEY"):
        return
    raise SystemExit(
        "Set ANTHROPIC_API_KEY for the Anthropic path (default for lyzr-docs-agent),\n"
        "OR set GITCLAW_MODEL_BASE_URL + OPENAI_API_KEY for an OpenAI-compatible "
        "endpoint, and re-run."
    )


async def main() -> None:
    _check_env()
    repo_url = os.environ.get("GAP_REPO_URL", _DEFAULT_REPO)
    print(f"→ source repo : {repo_url}")
    print(f"→ engine      : gitagent (auto-dispatch on model prefix)")
    print(f"→ identity    : gap (reads agent.yaml + SOUL.md + RULES.md)")
    backend_hint = "Anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "OpenAI"
    print(f"→ likely backend: {backend_hint} (final selection depends on agent.yaml's model)")
    print("─" * 60)

    async with ComputerAgent(
        source=repo_url,
        engine="gitagent",
        runtime="local",
        identity_loader="gap",
        agent_description="GAP-shaped Lyzr docs agent — 0.2.0 smoke",
    ) as agent:
        for i, prompt in enumerate(_TURNS, start=1):
            print(f"\n──── turn {i} ────")
            print(f"user: {prompt}")
            result = await agent.chat(prompt)
            answer = ""
            for msg in reversed(result.messages):
                content = getattr(msg, "content", None)
                if isinstance(content, str) and content.strip():
                    answer = content.strip()
                    break
                # Anthropic returns content as a list of blocks.
                if isinstance(content, list):
                    for block in content:
                        btype = getattr(block, "type", None)
                        if btype == "text":
                            text = getattr(block, "text", None)
                            if isinstance(text, str) and text.strip():
                                answer = text.strip()
                                break
                    if answer:
                        break
            print(f"asst: {answer}")
            print(
                f"      [tok_in={result.usage.input_tokens} "
                f"tok_out={result.usage.output_tokens} "
                f"duration={result.duration_ms:.0f}ms]"
            )

    print("\n" + "─" * 60)
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
