"""Same Python code, traces flow to New Relic instead of a local collector.

Only the env vars differ from ``with_otel.py`` — no code change. This is the
"later I can choose any platform" property of the OTel sink.

Run:
    pip install 'computer-agent-py[otel]'
    export OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp.nr-data.net
    export OTEL_EXPORTER_OTLP_HEADERS=api-key=<NEW_RELIC_LICENSE_KEY>
    export OTEL_SERVICE_NAME=nordassist-qa
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/with_new_relic.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from computeragent import ClaudeAgentOptions, query
from computeragent.types import ResultMessage


async def main() -> None:
    # No `configure(...)` call here — the OTel sink auto-attaches because
    # OTEL_EXPORTER_OTLP_ENDPOINT is set in the environment.
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in os.environ, "set OTEL_EXPORTER_OTLP_ENDPOINT before running"

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
                print("done; check New Relic for invoke_agent spans")


if __name__ == "__main__":
    asyncio.run(main())
