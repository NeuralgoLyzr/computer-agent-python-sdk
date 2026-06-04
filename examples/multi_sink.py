"""Both OTel and AgentOS sinks active, with PII redaction + guardrails in front.

NordAssist's plausible prod config: every run emits an OTel trace to New Relic
(or ClickHouse, or wherever), and AgentOS shows the agent + logs in the
frontend. PII is masked at the package boundary so neither sink ever sees
plaintext emails / phone numbers / etc.

Run:
    pip install 'computer-agent-py[all]'
    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
    export AGENTOS_INGEST_URL=http://localhost:8788/agentos/api/ingest/events
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/multi_sink.py
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
from computeragent.telemetry.sinks import AgentOSHttpSink, OtelSink
from computeragent.types import ResultMessage


async def main() -> None:
    configure(
        middleware=[
            PiiRedactor(strategy="hash"),
            GuardrailFilter(
                max_attribute_length=4096,
                tool_name_allowlist={"Read", "Glob", "Grep", "mcp__nordassist-tools__*"},
                cost_ceiling_usd=1.50,
            ),
        ],
        sinks=[
            OtelSink(),  # picks up OTEL_EXPORTER_OTLP_ENDPOINT
            AgentOSHttpSink(url=os.environ["AGENTOS_INGEST_URL"]),
        ],
        service_name="nordassist-qa",
    )

    with tempfile.TemporaryDirectory() as session_dir:
        options = ClaudeAgentOptions(
            model=os.environ.get("AGENT_MODEL", "claude-sonnet-4-5"),
            cwd=session_dir,
            system_prompt="You are NordAssist. Answer concisely.",
            allowed_tools=["Read", "Glob", "Grep"],
            mcp_servers={},
            permission_mode="bypassPermissions",
            add_dirs=[],
            env={k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC_", "AWS_"))},
        )

        async for message in query(prompt="Say hi in five words.", options=options):
            if isinstance(message, ResultMessage):
                print("done.", "cost=$%.4f turns=%s" % (message.total_cost_usd or 0.0, message.num_turns))


if __name__ == "__main__":
    asyncio.run(main())
