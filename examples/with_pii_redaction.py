"""Register a ``PiiRedactor`` so emails, phones, AWS keys etc. never reach a sink.

The agent here is asked to ``Read`` a file whose path contains an email. Even
though the upstream message stream carries the verbatim email in the
``tool_use`` block, by the time the OTel span / AgentOS log doc is written
the value is masked.

Run:
    pip install 'computer-agent-py[otel]'
    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318  # optional
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/with_pii_redaction.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from computeragent import ClaudeAgentOptions, PiiRedactor, configure, query
from computeragent.telemetry.sinks import OtelSink
from computeragent.types import ResultMessage


async def main() -> None:
    # `hash` strategy is deterministic — useful when you need to correlate
    # the same value across spans without ever shipping the plaintext.
    configure(
        middleware=[
            PiiRedactor(
                strategy="hash",
                extra_patterns=[r"BADGE-\d{6}"],
                path_denylist=["payload.tool_input.api_key"],
            ),
        ],
        sinks=[OtelSink()],  # picks up OTEL_EXPORTER_OTLP_ENDPOINT if set
    )

    with tempfile.TemporaryDirectory() as session_dir:
        # Pretend prompt contains PII the agent must touch.
        prompt = (
            "User alice@example.com asked about routing. Please cite their "
            "ticket BADGE-123456 in your answer."
        )

        options = ClaudeAgentOptions(
            model=os.environ.get("AGENT_MODEL", "claude-sonnet-4-5"),
            cwd=session_dir,
            system_prompt="You are an assistant. Answer concisely.",
            allowed_tools=["Read"],
            mcp_servers={},
            permission_mode="bypassPermissions",
            add_dirs=[],
            env={k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC_", "AWS_"))},
        )

        answer = ""
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                answer = message.result or ""
        print("Final answer (note: PII was redacted in telemetry, NOT in this stdout print):")
        print(answer)


if __name__ == "__main__":
    asyncio.run(main())
