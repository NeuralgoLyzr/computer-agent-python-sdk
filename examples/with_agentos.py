"""Make this Python agent show up in AgentOS.

Set ``AGENTOS_INGEST_URL`` (and optionally ``AGENTOS_INGEST_TOKEN``); the
``AgentOSHttpSink`` POSTs telemetry events to the AgentOS server, which writes
``agent_registry`` (one doc per agent), ``agent_logs`` (one doc per run),
``sessions`` + ``chat_sessions`` (the transcript the Chat tab renders), and
``agent_messages`` (per-event archive). No Mongo creds needed on this side.

Run:
    pip install 'computer-agent-py[agentos]'
    export AGENTOS_INGEST_URL=http://localhost:8788/agentos/api/ingest/events
    export AGENTOS_INGEST_TOKEN=dev-token        # only if the server sets it
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/with_agentos.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from computeragent import ClaudeAgentOptions, configure, query
from computeragent.telemetry.sinks import AgentOSHttpSink
from computeragent.types import ResultMessage


async def main() -> None:
    ingest_url = os.environ["AGENTOS_INGEST_URL"]
    configure(
        sinks=[AgentOSHttpSink(url=ingest_url)],
    )

    with tempfile.TemporaryDirectory() as session_dir:
        options = ClaudeAgentOptions(
            model=os.environ.get("AGENT_MODEL", "claude-sonnet-4-5"),
            cwd=session_dir,
            system_prompt="You are AcmeAssist. Answer concisely.",
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
