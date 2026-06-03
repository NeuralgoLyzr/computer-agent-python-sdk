# Live e2e against AgentOS

Stand up the ComputerAgent stack (`mongo + clickhouse + otel-collector + harness + agentos-server + spa`), run our Python demo, and verify every surface — Agents list, Logs tab, Chat tab, Observability tab — lights up.

## Prerequisites

- Docker (with Compose v2)
- `claude` CLI authenticated, **or** `ANTHROPIC_API_KEY` set in the environment, **or** Bedrock credentials available
- Python 3.10+ with `uv`

## One-time setup

1. Clone the ComputerAgent monorepo (sibling to `computeragent-py/`):

   ```text
   lyzr-experiments/
     ├─ ComputerAgent/          ← TS monorepo with docker-compose.yml
     └─ computeragent-py/       ← this package
   ```

2. Copy and edit env in the **monorepo root**:

   ```bash
   cd <ComputerAgent>
   cp .env.example .env
   $EDITOR .env
   #   API_AUTH_USER=admin
   #   API_AUTH_PASS=<choose one>
   #   ANTHROPIC_API_KEY=sk-ant-...
   ```

3. Install our Python package with all extras:

   ```bash
   cd <computeragent-py>
   uv sync --all-extras --dev
   ```

## Bring up the stack

Two flavors depending on your `.env`:

### Flavor A — fully local (local Mongo container)

If you want everything to run on your laptop with no remote services, ensure your `.env` sets `MONGO_URL=mongodb://mongo:27017` and `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`. Then:

```bash
cd <ComputerAgent>
docker compose \
    -f docker-compose.yml \
    -f ../computeragent-py/examples/e2e/docker-compose.override.yml \
    up --build
```

Uncomment the `mongo` service in `docker-compose.override.yml` first so port 27017 is exposed to the host.

### Flavor B — remote DocumentDB + New Relic (matches the repo's current `.env`)

This is what the existing `.env` in `ComputerAgent/` already sets up. The local stack only runs `agentos` + `spa`; everything else lives in AWS / New Relic. Skip the local mongo/clickhouse/otel-collector/harness deps to avoid port conflicts:

```bash
cd <ComputerAgent>
docker compose -f docker-compose.yml up --build --no-deps agentos spa -d
```

Two services should reach `running` state. `agentos-server` reads from your remote DocumentDB; SPA queries traces from New Relic via `TRACE_BACKEND=newrelic`.

Open `http://localhost:8080` — the AgentOS SPA. Log in with the creds from `.env`. Agents list will be empty.

## Run the demo

In a second terminal:

```bash
cd <computeragent-py>
uv run python examples/e2e/run_live_demo.py
```

Expected output:

```
→ Mongo URL: mongodb://localhost:27017  (db=computeragent-test)
→ OTel endpoint: http://localhost:4318
→ Running agent with model=claude-sonnet-4-5 cwd=/tmp/ca-e2e-...

answer: Routing uses BM25 scoring against team metadata indexes; ...
turns:  4
cost:   $0.0123

================================================================
AgentOS Mongo summary (db=computeragent-test)
================================================================
  agent_registry : 1 doc(s)
  agent_logs     : 1 doc(s)
  sessions       : 1 doc(s)
  agent_messages : 8 doc(s)
  agent_messages by kind: {'assistant_message': 2, 'tool_use': 2,
                            'tool_result': 2, 'usage_snapshot': 2}
...
```

## Verify in the SPA

Refresh `http://localhost:8080`:

| Surface | Expected |
|---|---|
| **Agents list** (sidebar) | New agent ("You are AcmeAssist..." truncated) under the **Library** filter |
| **Agent → Logs tab** | One row, `source: library`, with the redacted prompt as `query` and a real reply. Click → expands inline showing full query/reply |
| **Agent → Chat tab** | A session entry exists; opening it shows chat bubbles (user prompt seed + assistant answer) |
| **Agent → Observability tab** | Trace tree: `invoke_agent` → `chat` → `execute_tool` × N. `gen_ai.*` attributes populated |

## Direct Mongo verification (optional)

```bash
docker compose exec mongo mongosh computeragent-test --eval '
  db.agent_messages.find({}, {kind: 1, sessionId: 1, "payload.tool_name": 1}).limit(20).toArray()
'
```

You should see per-kind docs. Hashed PII (no plaintext `alice@example.com`).

## Failure-mode checks

- `docker compose stop mongo` mid-run → next run warns in stderr but the agent itself completes.
- Set `OTEL_EXPORTER_OTLP_ENDPOINT=` (empty) before running → console exporter prints spans to stdout; Observability tab gets nothing new; Mongo writes still happen.
- For policy-gating: edit `run_live_demo.py` to add `can_use_tool=PolicyToolAuthorizer(...)` with an unreachable OPA URL → every tool call denied; agent transcript shows denial messages.

## Cleanup

```bash
cd <ComputerAgent>
docker compose -f docker-compose.yml \
               -f ../computeragent-py/examples/e2e/docker-compose.override.yml \
               down -v
```

## See also

- [destinations.md](destinations.md) — point OTel at New Relic / Datadog / Honeycomb / Tempo / Jaeger by changing env vars only.
- [../with_otel.py](../with_otel.py), [../with_new_relic.py](../with_new_relic.py), [../with_datadog.py](../with_datadog.py) — single-vendor variants.
- [../with_message_archive.py](../with_message_archive.py) — the full pipeline (OTel + AgentOS + per-message archive) but without docker-compose.
