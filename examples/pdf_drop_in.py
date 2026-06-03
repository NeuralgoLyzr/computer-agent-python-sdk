"""NordAssist QA worker — verbatim from the architecture PDF, page 4-5.

The only difference between this file and the production NordAssist activity
is the two import lines. Everything else — the ``ClaudeAgentOptions``
construction, the ``async for message in query(...)`` loop, the
``isinstance(message, ResultMessage)`` check, the field reads — is unchanged.

Run:
    pip install 'computer-agent-py[otel,agentos]'
    # ensure the `claude` CLI is on PATH and Bedrock creds are set
    export ANTHROPIC_API_KEY=sk-ant-...   # or AWS_* for Bedrock
    python examples/pdf_drop_in.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

# ── the two imports a NordAssist engineer changes ─────────────────────────
from computeragent import ClaudeAgentOptions, query
from computeragent.types import ResultMessage
# ──────────────────────────────────────────────────────────────────────────


async def main() -> None:
    question = "Summarize how a Temporal workflow routes a question via BM25."

    with tempfile.TemporaryDirectory() as session_dir:
        # NordAssist writes CLAUDE.md + .index/ here in prod. For a minimal
        # demo we just give the agent a hint via the system prompt.
        os.makedirs(os.path.join(session_dir, ".index"), exist_ok=True)

        options = ClaudeAgentOptions(
            model=os.environ.get("AGENT_MODEL", "claude-sonnet-4-5"),
            cwd=session_dir,
            system_prompt=(
                "You are NordAssist, an AI assistant for engineers. "
                "Answer concisely."
            ),
            allowed_tools=["Read", "Glob", "Grep"],
            mcp_servers={},  # NordAssist passes its in-process MCP here
            permission_mode="bypassPermissions",
            add_dirs=[],
            env={
                k: v
                for k, v in os.environ.items()
                if k in {"ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION", "AWS_SESSION_TOKEN"}
            },
        )

        answer = ""
        turns = 0
        cost = 0.0
        async for message in query(prompt=question, options=options):
            if isinstance(message, ResultMessage):
                answer = message.result or ""
                turns = message.num_turns or 0
                cost = message.total_cost_usd or 0.0

        print(f"answer ({turns} turns, ${cost:.4f}):")
        print(answer)


if __name__ == "__main__":
    asyncio.run(main())
