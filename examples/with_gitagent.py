"""Harness-mode example with the ``gitagent`` engine (0.2.0+).

The Python ``GitAgentEngine`` re-implements the TypeScript ``engine-gitagent``
loop against an OpenAI-compatible endpoint. Env contract matches the TS
engine 1:1 so a single .env serves both languages:

* ``GITCLAW_MODEL_BASE_URL`` — base URL (e.g. ``https://api.lyzr.ai/v1``)
* ``OPENAI_API_KEY`` — bearer for that URL

Install the optional extra::

    pip install 'computer-agent-py[gitagent]'

Tool execution is **MCP-only** in 0.2.0 — wire an MCP filesystem server
through ``options['mcp_servers']`` if you need ``Read`` / ``Write`` /
``Bash``. Built-in file tools land in 0.3.0.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from computeragent import ComputerAgent


async def main() -> None:
    if not os.environ.get("GITCLAW_MODEL_BASE_URL"):
        raise SystemExit(
            "set GITCLAW_MODEL_BASE_URL (e.g. https://api.lyzr.ai/v1) and re-run"
        )
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("set OPENAI_API_KEY and re-run")

    async with ComputerAgent(
        source=Path.cwd(),
        engine="gitagent",
        runtime="local",
        agent_name="GitAgentQuickstart",
        agent_description="0.2.0 gitagent smoke test.",
        options={
            "model": os.environ.get("GITAGENT_MODEL", "gpt-4o-mini"),
            "system_prompt": "You are a terse assistant. Answer in one sentence.",
            "max_turns": 3,
        },
    ) as agent:
        result = await agent.chat("Say hello and tell me what 2 + 2 equals.")

    print("─" * 60)
    print(f"session_id   : {result.session_id}")
    print(f"prompt tok   : {result.usage.input_tokens}")
    print(f"completion   : {result.usage.output_tokens}")
    print(f"duration_ms  : {result.duration_ms:.0f}")
    print(f"messages     : {len(result.messages)}")
    print("─" * 60)
    for msg in reversed(result.messages):
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            print("answer:", content.strip())
            break


if __name__ == "__main__":
    asyncio.run(main())
