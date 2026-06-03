"""End-to-end live demo against the AgentOS stack.

Prerequisites:

1. The ComputerAgent docker-compose stack is running with the overlay in this
   directory exposing Mongo + OTel ports:

        cd <ComputerAgent monorepo>
        docker compose \\
            -f docker-compose.yml \\
            -f ../computeragent-py/examples/e2e/docker-compose.override.yml \\
            up --build

2. The `claude` CLI is on PATH and authenticated (or ``ANTHROPIC_API_KEY`` /
   Bedrock creds are in the environment).

3. The package is installed editable with the right extras::

        pip install -e '.[all]'   # otel + agentos + cedar

Run:

    python examples/e2e/run_live_demo.py

What it does:
* Configures the telemetry pipeline with PII redaction + guardrails.
* Fans out to OtelSink (→ otel-collector → ClickHouse → SPA Observability),
  AgentRegistrySink (→ agent_registry + agent_logs + sessions in Mongo),
  MongoMessageSink (→ agent_messages in Mongo, per-message archive).
* Runs a small agent that reads a couple of files in a scratch dir, so we
  generate tool_use + tool_result + usage_snapshot events.
* Prints a mongosh-style summary of what landed in each collection.

Then open http://localhost:8080 in a browser → log in → click the new agent
under the "Library" filter → verify Logs / Chat / Observability tabs.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient

from computeragent import (
    ClaudeAgentOptions,
    GuardrailFilter,
    PiiRedactor,
    configure,
    query,
)
from computeragent.telemetry.sinks import AgentRegistrySink, MongoMessageSink, OtelSink
from computeragent.types import ResultMessage

# Honour both the package's preferred env names AND the names the existing
# ComputerAgent docker-compose stack already uses (MONGO_URL / MONGO_DATABASE),
# so the Python script and the running stack point at the same Mongo without
# any duplicated env setup.
MONGO_URL = (
    os.environ.get("AGENTOS_MONGO_URL")
    or os.environ.get("MONGO_URL")
    or "mongodb://localhost:27017"
)
MONGO_DB = (
    os.environ.get("AGENTOS_MONGO_DB")
    or os.environ.get("MONGO_DATABASE")
    or "computeragent-test"
)
OTEL_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")


def _seed_session_dir() -> Path:
    """Stage a tiny scratch dir so the agent has something to Read/Glob/Grep."""
    d = Path(tempfile.mkdtemp(prefix="ca-e2e-"))
    (d / "CLAUDE.md").write_text(
        "Answer concisely (2 sentences max).\n"
        "Files in this directory describe the routing system.\n"
    )
    (d / "routing.md").write_text(
        "Routing uses BM25 scoring against per-team metadata, then expands "
        "the top-K teams. A phrase bonus boosts exact substring matches.\n"
    )
    (d / "heartbeats.md").write_text(
        "Long-running activities heartbeat to Temporal every 30 seconds.\n"
    )
    return d


async def _summary(client: AsyncIOMotorClient) -> None:
    """mongosh-style end-of-run dump so the demo is self-verifying."""
    db = client[MONGO_DB]
    reg = await db["agent_registry"].count_documents({})
    logs = await db["agent_logs"].count_documents({})
    sess = await db["sessions"].count_documents({})
    msgs = await db["agent_messages"].count_documents({})
    print()
    print("=" * 64)
    print("AgentOS Mongo summary (db=%s)" % MONGO_DB)
    print("=" * 64)
    print(f"  agent_registry : {reg} doc(s)")
    print(f"  agent_logs     : {logs} doc(s)")
    print(f"  sessions       : {sess} doc(s)")
    print(f"  agent_messages : {msgs} doc(s)")

    if msgs:
        kinds: dict[str, int] = {}
        async for d in db["agent_messages"].find({}, {"kind": 1, "_id": 0}):
            kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
        print(f"  agent_messages by kind: {kinds}")

    last_log = await db["agent_logs"].find_one(sort=[("ts", -1)])
    if last_log:
        print()
        print("Most recent agent_logs row:")
        print(f"  agentName : {last_log.get('agentName')}")
        print(f"  source    : {last_log.get('source')}")
        print(f"  ok        : {last_log.get('ok')}")
        print(f"  costUsd   : {last_log.get('costUsd')}")
        print(f"  query     : {(last_log.get('query') or '')[:80]!r}")
        print(f"  reply     : {(last_log.get('reply') or '')[:80]!r}")

    last_sess = await db["sessions"].find_one(sort=[("createdAt", -1)])
    if last_sess:
        print()
        print(f"Most recent sessions row: {last_sess['_id']}")
        print(f"  entries: {len(last_sess.get('entries', []))}")
        for i, e in enumerate(last_sess.get("entries", [])[:6]):
            text = (e.get("text") or "")[:60]
            print(f"    [{i}] {e.get('type'):>9}: {text!r}")


async def main() -> None:
    print(f"→ Mongo URL: {MONGO_URL}  (db={MONGO_DB})")
    print(f"→ OTel endpoint: {OTEL_ENDPOINT}")

    configure(
        middleware=[
            # Hash strategy → deterministic mask so we can spot redactions
            # in the SPA without leaking the original PII to the backend.
            PiiRedactor(strategy="hash"),
            GuardrailFilter(
                max_attribute_length=4096,
                tool_name_allowlist={"Read", "Glob", "Grep"},
            ),
        ],
        sinks=[
            OtelSink(),  # auto-picks up OTEL_EXPORTER_OTLP_ENDPOINT
            AgentRegistrySink(mongo_url=MONGO_URL, database=MONGO_DB),
            MongoMessageSink(mongo_url=MONGO_URL, database=MONGO_DB),
        ],
        service_name="computeragent-e2e-demo",
    )

    session_dir = _seed_session_dir()
    options = ClaudeAgentOptions(
        model=os.environ.get("AGENT_MODEL", "claude-sonnet-4-5"),
        cwd=str(session_dir),
        system_prompt=(
            "You are AcmeAssist, an AI assistant for engineers. "
            "Read CLAUDE.md and the routing docs, then answer."
        ),
        allowed_tools=["Read", "Glob", "Grep"],
        mcp_servers={},
        permission_mode="bypassPermissions",
        add_dirs=[],
        env={k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC_", "AWS_"))},
    )

    prompt = (
        "Summarize how routing works in one sentence. "
        "Note: requested by alice@example.com (BADGE-123456)."
    )

    print(f"→ Running agent with model={options.model} cwd={session_dir}")
    print()
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            print(f"answer: {message.result}")
            print(f"turns:  {message.num_turns}")
            print(f"cost:   ${message.total_cost_usd or 0.0:.4f}")

    # Mongosh-style verification.
    client: AsyncIOMotorClient = AsyncIOMotorClient(MONGO_URL)
    try:
        await _summary(client)
    finally:
        client.close()

    print()
    print("Now open http://localhost:8080 → log in → click the new agent")
    print("under the 'Library' filter → verify Logs / Chat / Observability tabs.")


if __name__ == "__main__":
    asyncio.run(main())
