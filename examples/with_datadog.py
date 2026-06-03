"""Same Python code as ``with_new_relic.py`` — different env vars, traces
flow to Datadog instead. Demonstrates the vendor-neutral OTLP property.

Two ways to wire Datadog:

(A) Via a local Datadog Agent with OTLP intake enabled (recommended).
    Edit ``datadog.yaml``::

        otlp_config:
          receiver:
            protocols:
              http:
                endpoint: 0.0.0.0:4318

    Then::

        pip install 'computer-agent-py[otel]'
        export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
        export OTEL_SERVICE_NAME=nordassist-qa
        export ANTHROPIC_API_KEY=sk-ant-...
        python examples/with_datadog.py

(B) Direct OTLP intake (no agent). Requires a DD API key::

        export OTEL_EXPORTER_OTLP_ENDPOINT=https://trace.agent.datadoghq.com
        export OTEL_EXPORTER_OTLP_HEADERS=DD-API-KEY=<key>
        export OTEL_SERVICE_NAME=nordassist-qa
        python examples/with_datadog.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from computeragent import ClaudeAgentOptions, query
from computeragent.types import ResultMessage


async def main() -> None:
    # No explicit configure() call — OtelSink auto-attaches because
    # OTEL_EXPORTER_OTLP_ENDPOINT is set.
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in os.environ, (
        "Set OTEL_EXPORTER_OTLP_ENDPOINT before running "
        "(see examples/e2e/destinations.md for Datadog recipes)."
    )

    with tempfile.TemporaryDirectory() as session_dir:
        options = ClaudeAgentOptions(
            model=os.environ.get("AGENT_MODEL", "claude-sonnet-4-5"),
            cwd=session_dir,
            system_prompt="Answer concisely.",
            allowed_tools=["Read"],
            mcp_servers={},
            permission_mode="bypassPermissions",
            add_dirs=[],
            env={k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC_", "AWS_"))},
        )

        async for message in query(prompt="Say hi.", options=options):
            if isinstance(message, ResultMessage):
                print("done; check Datadog APM for invoke_agent spans")


if __name__ == "__main__":
    asyncio.run(main())
