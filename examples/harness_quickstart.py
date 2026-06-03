"""Minimal harness-mode example (0.2.0+).

Spins up a :class:`ComputerAgent` against the bundled
``claude-agent-sdk`` engine and the ``local`` substrate, sends one
prompt, prints the reply.

Run with ``ANTHROPIC_API_KEY`` exported (or an active ``claude`` CLI
session); the substrate uses the current working directory as the
agent's workspace.

The 0.1.x ``query()`` / ``ClaudeSDKClient`` drop-in surface is untouched
— see ``pdf_drop_in.py`` for that flow.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from computeragent import ComputerAgent


async def main() -> None:
    async with ComputerAgent(
        source=Path.cwd(),
        engine="claude-agent-sdk",
        runtime="local",
        agent_name="HarnessQuickstart",
        agent_description="0.2.0 harness smoke test.",
        options={
            "system_prompt": "You are a terse assistant. Answer in one sentence.",
            "allowed_tools": ["Read", "Glob", "Grep"],
            "permission_mode": "bypassPermissions",
            "max_turns": 3,
        },
    ) as agent:
        result = await agent.chat("List the files in the current directory.")

    print("─" * 60)
    print(f"session_id    : {result.session_id}")
    print(f"input_tokens  : {result.usage.input_tokens}")
    print(f"output_tokens : {result.usage.output_tokens}")
    print(f"cost_usd      : {result.usage.cost_usd:.4f}")
    print(f"duration_ms   : {result.duration_ms:.0f}")
    print(f"messages      : {len(result.messages)}")
    print("─" * 60)
    # Print the last assistant text, if any.
    for msg in reversed(result.messages):
        content = getattr(msg, "content", None)
        if not content:
            continue
        text_parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                text_parts.append(text)
        if text_parts:
            print("answer:", "".join(text_parts))
            break


if __name__ == "__main__":
    asyncio.run(main())
