"""Full proxy stack — guardrails, OTel, AgentOS registry, AND per-message archive.

This is what AcmeAssist's worker can look like once everything is wired in.
Same agent code as the PDF; the only adds are:

* a telemetry pipeline that:
   - redacts PII before any sink sees it (PiiRedactor)
   - drops oversize / disallowed tools (GuardrailFilter)
   - emits to three sinks: OTel collector, AgentOS session log, and per-message archive
* tool-use authorization via OPA (commented out below — flip on when the
  OPA server is reachable)

Run:
    pip install -e '.[all]'
    export AGENTOS_MONGO_URL=mongodb://localhost:27017
    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   # optional
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/with_message_archive.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from computeragent import (
    ClaudeAgentOptions,
    GuardrailFilter,
    PiiRedactor,
    configure,
    query,
)
from computeragent.telemetry.sinks import AgentRegistrySink, MongoMessageSink, OtelSink
from computeragent.types import ResultMessage


async def main() -> None:
    mongo_url = os.environ["AGENTOS_MONGO_URL"]

    configure(
        middleware=[
            # Guardrails run BEFORE any sink — the archive only ever sees
            # redacted payloads. Compliance default.
            PiiRedactor(strategy="hash"),
            GuardrailFilter(
                max_attribute_length=4096,
                tool_name_allowlist={"Read", "Glob", "Grep", "mcp__acme-assist-tools__*"},
                cost_ceiling_usd=1.50,
            ),
        ],
        sinks=[
            OtelSink(),  # spans + metrics → your OTLP collector
            AgentRegistrySink(mongo_url=mongo_url),  # session rollups → agent_registry + agent_logs
            MongoMessageSink(mongo_url=mongo_url),  # per-message archive → agent_messages
        ],
        service_name="acme-assist-qa",
    )

    with tempfile.TemporaryDirectory() as session_dir:
        options = ClaudeAgentOptions(
            model=os.environ.get("AGENT_MODEL", "claude-sonnet-4-5"),
            cwd=session_dir,
            system_prompt="You are AcmeAssist. Answer concisely.",
            allowed_tools=["Read", "Glob", "Grep"],
            mcp_servers={},
            permission_mode="bypassPermissions",
            add_dirs=[],
            env={k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC_", "AWS_"))},
            # To enable policy gating, add `can_use_tool=PolicyToolAuthorizer(...)`
            # — see examples/with_opa_policy.py and examples/with_cedar_policy.py.
        )

        async for message in query(
            prompt="Say hi in 5 words; user contact: alice@example.com.", options=options
        ):
            if isinstance(message, ResultMessage):
                print("done. cost=$%.4f turns=%s" % (message.total_cost_usd or 0.0, message.num_turns))

    print(
        "Open AgentOS to inspect:\n"
        "  - agent_registry  : one doc per agent name\n"
        "  - agent_logs      : one doc per session (rollup)\n"
        "  - agent_messages  : one doc per message (full transcript, PII-redacted)"
    )


if __name__ == "__main__":
    asyncio.run(main())
