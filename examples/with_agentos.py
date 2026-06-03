"""Make this Python agent show up in AgentOS.

Set ``AGENTOS_MONGO_URL``; the ``AgentRegistrySink`` will upsert into
``agent_registry`` (one doc per agent name) and append to ``agent_logs``
(one doc per run). The AgentOS frontend reads those collections without any
AgentOS-side changes.

Run:
    pip install 'computer-agent-py[agentos]'
    export AGENTOS_MONGO_URL=mongodb://localhost:27017
    export AGENTOS_MONGO_DB=agentos
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/with_agentos.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from computeragent import ClaudeAgentOptions, configure, query
from computeragent.telemetry.sinks import AgentRegistrySink
from computeragent.types import ResultMessage


async def main() -> None:
    mongo_url = os.environ["AGENTOS_MONGO_URL"]
    configure(
        sinks=[AgentRegistrySink(mongo_url=mongo_url)],
    )

    with tempfile.TemporaryDirectory() as session_dir:
        options = ClaudeAgentOptions(
            model=os.environ.get("AGENT_MODEL", "claude-sonnet-4-5"),
            cwd=session_dir,
            system_prompt="You are NordAssist. Answer concisely.",
            allowed_tools=["Read"],
            mcp_servers={},
            permission_mode="bypassPermissions",
            add_dirs=[],
            env={k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC_", "AWS_"))},
        )

        async for message in query(prompt="Say hi in five words.", options=options):
            if isinstance(message, ResultMessage):
                print("agent run complete; check AgentOS frontend → registry tab")


if __name__ == "__main__":
    asyncio.run(main())
