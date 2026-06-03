"""Emit OTel spans + metrics to a collector — same shapes as the TS
``@computeragent/observability`` package.

Pair with the ComputerAgent monorepo's ``examples/otel-collector/`` stack:

    cd <ComputerAgent>/examples/otel-collector
    docker compose up -d

then:

    pip install 'computer-agent-py[otel]'
    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/with_otel.py

The bundled collector fans out to a debug exporter (stdout) and ClickHouse.
After the run, the SQL in ``examples/otel-collector/genai-materialized-columns.sql``
returns Python-emitted rows identical in shape to TS-emitted ones.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from computeragent import ClaudeAgentOptions, configure, query
from computeragent.telemetry.sinks import OtelSink
from computeragent.types import ResultMessage


async def main() -> None:
    configure(
        service_name="computeragent-python-demo",
        sinks=[OtelSink()],
        capture_content=True,
        capture_content_mode="both",
    )

    with tempfile.TemporaryDirectory() as session_dir:
        options = ClaudeAgentOptions(
            model=os.environ.get("AGENT_MODEL", "claude-sonnet-4-5"),
            cwd=session_dir,
            system_prompt="Answer concisely.",
            allowed_tools=["Read", "Glob", "Grep"],
            mcp_servers={},
            permission_mode="bypassPermissions",
            add_dirs=[],
            env={k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC_", "AWS_"))},
        )

        async for message in query(prompt="Write a one-line haiku about Temporal.", options=options):
            if isinstance(message, ResultMessage):
                print("done.", "cost=$%.4f turns=%s" % (message.total_cost_usd or 0.0, message.num_turns))


if __name__ == "__main__":
    asyncio.run(main())
