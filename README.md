# computer-agent-py

[![PyPI](https://img.shields.io/pypi/v/computer-agent-py.svg)](https://pypi.org/project/computer-agent-py/)
[![Python](https://img.shields.io/pypi/pyversions/computer-agent-py.svg)](https://pypi.org/project/computer-agent-py/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Drop-in replacement for [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/)** — change the import line, get a proxied telemetry pipeline with PII redaction, configurable OTel export, policy-based tool authorization, and full AgentOS integration for free.

**New in 0.2.0** — a Python port of the TypeScript [`ComputerAgent`](../ComputerAgent/) harness alongside the drop-in proxy. Same vocabulary (engine / substrate / session-store / identity-loader), `ComputerAgent` + `run_task()` entry points, two built-in engines (`claude-agent-sdk`, `gitagent`). The 0.1.x drop-in surface is untouched — both modes co-exist.

```diff
- from claude_agent_sdk import ClaudeAgentOptions, query
- from claude_agent_sdk.types import ResultMessage
+ from computeragent import ClaudeAgentOptions, query
+ from computeragent.types import ResultMessage
```

Every other line stays identical. `isinstance(msg, ResultMessage)` still works. The `claude` CLI subprocess, AWS Bedrock auth, MCP servers, permission modes, `cwd`, `add_dirs` — all behave exactly as the upstream SDK does.

> **Package vs import name** — PyPI distribution is `computer-agent-py` (hyphens for the wheel); the import name is `computeragent` (Python doesn't allow hyphens). So `pip install computer-agent-py` then `from computeragent import …`.

## Why use this

Adopting `computer-agent-py` in place of `claude-agent-sdk` gives you, without rewriting your agent code:

- **OpenTelemetry traces** following the GenAI Semantic Conventions, vendor-neutral (New Relic, Datadog, ClickHouse, Honeycomb, Tempo, Jaeger — one env-var change).
- **PII redaction** at the package boundary — email, phone, SSN, credit cards, AWS keys redacted before anything reaches a sink.
- **Generic guardrails** — attribute truncation, tool allowlists, per-session cost ceilings, content filters.
- **Policy-based tool-use authorization** — gate every tool call through OPA (remote) or Cedar (in-process). Fail-closed by default.
- **AgentOS visibility** — write to the same Mongo collections (`agent_registry`, `agent_logs`, `agent_messages`, `sessions`, `slack_threads`) the AgentOS frontend already reads. Library-mode agents show up in the Agents list, Logs tab, Chat transcript, and (with the harness running) live chat sandboxes.
- **Per-message archive** — every message, tool call, and policy decision archived to `agent_messages` for replay, RAG, and forensic audit.

## Install

```bash
pip install computer-agent-py                     # core drop-in + OPA policy engine
pip install 'computer-agent-py[otel]'             # + OpenTelemetry sink
pip install 'computer-agent-py[agentos]'          # + AgentOS Mongo sinks
pip install 'computer-agent-py[cedar]'            # + Cedar policy engine (in-process)
pip install 'computer-agent-py[gitagent]'         # + gitagent engine (Anthropic + OpenAI dispatch) + GAP loader
pip install 'computer-agent-py[gap]'              # + GAP identity loader only (pyyaml; subset of [gitagent])
pip install 'computer-agent-py[all]'              # everything
```

**Prerequisite** — same as upstream: the `claude` CLI binary on `PATH` and Anthropic / Bedrock credentials in the environment.

## Two modes

`computer-agent-py` ships two co-existing surfaces. Use whichever fits the call site — they share the same telemetry pipeline and policy authorizer.

### Mode 1 — drop-in proxy (0.1.x, unchanged)

The default for callers who want minimum-friction observability over `claude-agent-sdk`. Swap the import line and every `query(...)` / `ClaudeSDKClient(...)` call flows through PII redaction → guardrails → OTel + AgentOS sinks.

```python
from computeragent import ClaudeAgentOptions, query

async for msg in query(prompt="...", options=ClaudeAgentOptions(...)):
    ...
```

### Mode 2 — harness (new in 0.2.0)

A Python port of the TypeScript [`ComputerAgent`](../ComputerAgent/) stack. Four orthogonal swappable axes — engine / substrate / session-store / identity-loader — plus a `ComputerAgent` class and a `run_task()` convenience.

```python
from pathlib import Path
from computeragent import ComputerAgent

async with ComputerAgent(
    source=Path.cwd(),
    engine="claude-agent-sdk",   # or "gitagent" for OpenAI-compatible endpoints
    runtime="local",
    options={"system_prompt": "You are terse.", "allowed_tools": ["Read", "Glob"]},
) as agent:
    result = await agent.chat("List the files in this directory.")
    print(result.messages, result.usage)
```

Built-ins shipped in 0.2.0:

| Axis | Built-ins | Notes |
|---|---|---|
| `engine` | `claude-agent-sdk`, `gitagent` | gitagent reads `GITCLAW_MODEL_BASE_URL` + `OPENAI_API_KEY`. MCP-only tools in 0.2.0. |
| `runtime` (substrate) | `local` | Caller-owned `Path`, or git-clone an `https://` / `git@` `.git` URL into a temp dir. |
| `session_store` | `memory` | In-process dict. Mongo + SQLite stores in 0.3.0. |
| `identity_loader` | `passthrough` | Treats inline dicts as manifests. GAP-style loaders in 0.3.0. |

Plug-ins register at import time via `register_engine(name, instance)` etc. — third-party engines (e2b substrate, deepagents engine, mongo session store) drop in by implementing the matching Protocol in `computeragent.protocol_types`.

### Mode 2b — remote harness (run the engine on a server)

Set `harness_url` and the same `ComputerAgent` runs against a **remote harness
server** over HTTP instead of in-process — the engine/substrate/loader execute
there. The SDK becomes a thin, stateless client. Requires the `[remote]` extra
(`pip install 'computer-agent-py[remote]'`).

```python
from computeragent import ComputerAgent

# Bare harness protocol (@computeragent/harness-server, :7700) — default kind.
async with ComputerAgent(
    harness_url="http://harness:7700",
    harness="claude-agent-sdk",            # engine to run remotely
    source={"type": "git", "url": "github.com/org/agent"},
) as agent:
    async for event in agent.chat("Summarize the README"):
        ...                                # ca_session_started, sdk_message, ca_session_ended, …
    result = await agent.chat("…")         # ChatResult(messages, usage, session_id)

# ComputerAgent server (:8787) — pick the substrate per request.
async with ComputerAgent(
    harness_url="http://cas:8787", harness_kind="server", runtime="bwrap",
    harness="claude-agent-sdk", source={"type": "git", "url": "github.com/org/agent"},
) as agent:
    result = await agent.chat("…")
```

| `harness_kind` | Server | Substrate | Permissions |
|---|---|---|---|
| `"protocol"` (default) | `/v1/sessions/*` (harness-server, :7700) | server-side | `on_tool_call` round-trip |
| `"server"` | `/sandboxes` + `/run` (CAS, :8787) | client-chosen via `runtime` | server-side `permission_mode`/SRS |
| `"auto"` | probes `/v1/health` then `/health` | — | — |

The protocol layer is also usable directly: `from computeragent import HarnessClient, ComputerAgentServerClient`. `run_task(harness_url=...)` works the same for one-shots.

## Quickstart

```python
import asyncio
from computeragent import ClaudeAgentOptions, query
from computeragent.types import ResultMessage

async def main():
    options = ClaudeAgentOptions(
        model="claude-sonnet-4-5",
        system_prompt="You are a helpful assistant.",
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="bypassPermissions",
    )

    async for message in query(prompt="Summarize the README.md in this directory.", options=options):
        if isinstance(message, ResultMessage):
            print(f"answer ({message.num_turns} turns, ${message.total_cost_usd:.4f}):")
            print(message.result)

asyncio.run(main())
```

That's it — same shape as `claude-agent-sdk`. Telemetry is configured from env vars; without anything set, nothing leaves your process.

## Configure telemetry

### Env-driven (zero code change)

| Variable | Effect |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Base URL of an OTLP/HTTP backend. The sink appends `/v1/traces` and `/v1/metrics` automatically — pass the base. Unset → console exporter (debug). |
| `OTEL_EXPORTER_OTLP_HEADERS` | Comma-separated `key=value` (e.g. `api-key=NRRX-...` for New Relic). |
| `OTEL_SERVICE_NAME` | `service.name` attribute on every span. Default: `computeragent`. |
| `COMPUTERAGENT_OTEL` | `disabled` to suppress OtelSink. |
| `COMPUTERAGENT_OTEL_TEMPORALITY` | `delta` (default) or `cumulative`. SaaS OTLP intakes (New Relic, Datadog direct, Honeycomb) require delta; cumulative is only correct when exporting to a collector that aggregates downstream. |
| `AGENTOS_INGEST_URL` | When set + `[agentos]` installed, attaches `AgentOSHttpSink` — POSTs telemetry events to the AgentOS server, which writes the Mongo collections (registry, logs, sessions, chat_sessions, agent_messages). E.g. `http://host:8788/agentos/api/ingest/events`. |
| `AGENTOS_INGEST_TOKEN` | Optional bearer token sent as `Authorization: Bearer …`. Match the server's `AGENTOS_INGEST_TOKEN`. |
| `COMPUTERAGENT_CAPTURE_CONTENT` | `1` to include prompts/responses on OTel spans. Default: off. |
| `COMPUTERAGENT_CAPTURE_CONTENT_MODE` | `events` (default) \| `attributes` \| `both`. |

### Programmatic

```python
from computeragent import configure, PiiRedactor, GuardrailFilter
from computeragent.telemetry.sinks import OtelSink, AgentOSHttpSink

configure(
    middleware=[
        PiiRedactor(strategy="hash", extra_patterns=[r"BADGE-\d{6}"]),
        GuardrailFilter(
            max_attribute_length=4096,
            tool_name_allowlist={"Read", "Glob", "Grep", "mcp__acme-assist-tools__*"},
            cost_ceiling_usd=1.50,
        ),
    ],
    sinks=[
        OtelSink(),                                                     # picks up env
        AgentOSHttpSink(url="http://host:8788/agentos/api/ingest/events"),  # → server writes Mongo
    ],
)
```

## Vendor-neutral OTel destinations

The package emits standard OTLP — point it at any backend by setting two env vars. No code change.

| Destination | `OTEL_EXPORTER_OTLP_ENDPOINT` | `OTEL_EXPORTER_OTLP_HEADERS` |
|---|---|---|
| **New Relic** | `https://otlp.nr-data.net` | `api-key=<NR_LICENSE_KEY>` |
| **Datadog** (via DD Agent OTLP) | `http://localhost:4318` | _(unset; agent handles auth)_ |
| **Honeycomb** | `https://api.honeycomb.io` | `x-honeycomb-team=<KEY>` |
| **Grafana Cloud Tempo** | `https://tempo-prod-...grafana.net:443` | `authorization=Basic <base64>` |
| **Self-hosted Jaeger / Tempo / SigNoz** | `http://<host>:4318` | _(unset)_ |
| **Local console (debug)** | _(unset)_ | _(unset)_ |

Full recipe table — including direct New Relic / Datadog without an OTel collector — is in [`examples/e2e/destinations.md`](examples/e2e/destinations.md).

## Policy-based tool-use authorization

For agents that need stronger guardrails than `permission_mode`, attach an external policy engine. Activation is a single new option-field; the rest of the worker code is unchanged.

```python
from computeragent import ClaudeAgentOptions, PolicyPrincipal, PolicyResource, query
from computeragent.policy import OpaPolicyEngine, PolicyToolAuthorizer

opa = OpaPolicyEngine(
    url="http://opa.platform:8181",
    policy_path="computeragent/tools/allow",
    fail_mode="deny",   # default — engine errors deny the call
)
authorizer = PolicyToolAuthorizer(
    engine=opa,
    principal_resolver=lambda ctx: PolicyPrincipal(id="alice", groups=["engineer"]),
    resource_resolver=lambda ctx: PolicyResource(agent_name="acme-assist", model="claude-sonnet-4-5"),
    context_resolver=lambda ctx: {"env": "prod"},
)

options = ClaudeAgentOptions(
    ...,
    permission_mode="default",  # was "bypassPermissions"
    can_use_tool=authorizer,
)
```

Swap `OpaPolicyEngine` for `CedarPolicyEngine` (install with `pip install 'computer-agent-py[cedar]'`) and the worker code is identical:

```python
from computeragent.policy import CedarPolicyEngine

cedar = CedarPolicyEngine(
    policies=open("policies/computeragent.cedar").read(),
    fail_mode="deny",
)
authorizer = PolicyToolAuthorizer(engine=cedar, principal_resolver=..., ...)
```

Sample policies are in [`examples/policies/`](examples/policies/) — one Rego file for OPA, one Cedar file. Each policy receives a canonical `PolicyInput` shape (`principal`, `action`, `resource`, `context`) so the engine choice is purely operational.

Every authorization decision emits a `policy_decision` telemetry event — `OtelSink` annotates the active `execute_tool` span with `policy.decision`, `policy.reason`, `policy.engine`, and `policy.latency_ms` so security audits and span queries co-locate.

## AgentOS integration

When `[agentos]` is installed and `AGENTOS_INGEST_URL` is set, every agent run POSTs telemetry events to the AgentOS server, which projects them into the Mongo collections the frontend reads. The SDK holds no Mongo creds — the server owns the writes:

| Collection | Per | What's in it |
|---|---|---|
| `agent_registry` | agent name | Identity + harness + model + last-seen; idempotent upsert |
| `agent_logs` | run | Rolled-up query/reply + tokens + cost + ok/error — drives the Logs tab |
| `sessions` | session | `entries[]` of `{type, text}` chat-bubble messages — drives the Chat tab transcript |
| `chat_sessions` | session | Canonical session-index row (`agent`, `createdAt`, `lastMessageAt`) the session list + per-agent `sessionCount`/`lastActivity` read |
| `agent_messages` | message | Per-event archive (`user_message`, `assistant_message`, `tool_use`, `tool_result`, `usage_snapshot`, `policy_decision`, `system_message`) for replay, RAG, audit |

The projection runs in `@computeragent/agentos-server` (`POST /agentos/api/ingest/events`). Doc shapes match what the dashboard already reads — Python-driven agents show up in the same AgentOS UI as hosted TS agents, with no frontend change. The server is also the idempotency boundary: each event carries a stable `event_id`, so a retried batch never duplicates rows.

### Live chat for library-mode agents

Two registry shapes for `agent_registry.source`, depending on which mode emitted the event:

- **Drop-in proxy (`computeragent.query(...)` / `ClaudeSDKClient`)** writes a `type: "inline"` source with `files: {agent.yaml, CLAUDE.md}` derived from your `ClaudeAgentOptions`. If a user clicks "New Chat" on the agent in the AgentOS SPA, the harness can clone those files into a sandbox workdir and spawn a live conversation — same UX as hosted (git-sourced) agents.
- **Harness (`ComputerAgent`, new in 0.2.0)** writes a `type: "library"` source. There's no remote `harness-server` to proxy a chat-sandbox spawn to (the agent runs in your Python process), so this shape deliberately fails AgentOS's `hasResolvableSource()` check. The AgentOS UI hides the chat-sandbox button (via the matching `liveChatCapable` derived field on `GET /agents`); historical transcripts remain visible in the Chat tab.

Either way, every event still flows through OTel and the AgentOS ingest endpoint so the Agents list, Logs tab, and Chat transcript stay populated.

### DocumentDB compatibility

Prod deployments on AWS DocumentDB work without changes. The server-side projection uses only operators DocumentDB supports — `$set`, `$setOnInsert`, `$push`, `updateOne(upsert)`, `insertOne`. No aggregation pipelines, transactions, change streams, or TTL indexes. Set the server's `MONGO_URL` with the standard `tls=true&tlsCAFile=...` params and mount the DocumentDB CA bundle.

## Architecture

```
                 user code: from computeragent import query, ClaudeAgentOptions
                                          │
                                          ▼
                      computeragent._proxy.query  ──┐
                        │                            │
                        ▼                            │  PolicyToolAuthorizer
   claude_agent_sdk → claude CLI subprocess → Bedrock│ (OPA / Cedar)
                        │                            │  via can_use_tool
                        ▼                            │
                  yielded messages                   │
                        │                            │
                        ▼                            │
               TelemetryPipeline (taps stream)       │
                        │                            │
       ┌──── middleware ─────┐                       │
       │  PiiRedactor        │                       │
       │  GuardrailFilter    │  ◄────────────────────┘
       │  <user-defined>     │
       └────────┬────────────┘
                ▼
     ┌──── fan-out to sinks ───────────────────────────┐
     │                                                  │
     ▼            ▼                                     ▼
  OtelSink   AgentOSHttpSink                          <user>
     │            │
     ▼            │  POST /agentos/api/ingest/events
  OTLP backend    ▼
  (NR / DD /   agentos-server  ──▶  agent_registry / agent_logs /
   ClickHouse / (owns the Mongo      sessions / chat_sessions /
   Tempo …)     writes)              agent_messages  (drives AgentOS UI)
```

The proxy is a pure tap — messages are never modified or reordered. Sinks run as background tasks so a slow exporter never stalls the agent hot path; `query()`'s `finally` block awaits them with a 5 s default timeout. Telemetry never breaks an agent run: middleware and sink exceptions are absorbed and logged.

## Live e2e against AgentOS

[`examples/e2e/`](examples/e2e/) contains a recipe for standing up the full TypeScript stack (mongo + clickhouse + otel-collector + harness + agentos-server + SPA) via docker-compose and running this package against it. After ~60s of warm-up plus a 30s Python script run, you'll see the agent appear in the SPA's Agents list with `logCount`, `sessionCount`, `lastActivity`, and `activeSandboxes` populated; the Logs tab will show the rollup; the Chat tab will show the per-message transcript; the Observability tab will show the OTel trace tree. See [`examples/e2e/README.md`](examples/e2e/README.md).

## Examples

| File | Demonstrates |
|---|---|
| [`examples/pdf_drop_in.py`](examples/pdf_drop_in.py) | The minimum drop-in change |
| [`examples/with_otel.py`](examples/with_otel.py) | OTel pointed at a local collector |
| [`examples/with_new_relic.py`](examples/with_new_relic.py) | OTel pointed at New Relic (just env vars) |
| [`examples/with_datadog.py`](examples/with_datadog.py) | OTel pointed at Datadog |
| [`examples/with_agentos.py`](examples/with_agentos.py) | AgentOS HTTP ingest |
| [`examples/with_pii_redaction.py`](examples/with_pii_redaction.py) | PII middleware in front of every sink |
| [`examples/with_opa_policy.py`](examples/with_opa_policy.py) | OPA-gated tool use |
| [`examples/with_cedar_policy.py`](examples/with_cedar_policy.py) | Cedar-gated tool use (in-process) |
| [`examples/multi_sink.py`](examples/multi_sink.py) | All sinks + all guardrails together |
| [`examples/e2e/run_live_demo.py`](examples/e2e/run_live_demo.py) | Full live demo against the AgentOS docker-compose stack |

## Upstream pin

This release tracks **`claude-agent-sdk` 0.2.x**. The pinned upstream version is recorded in [`CHANGELOG.md`](CHANGELOG.md). Bump deliberately — wire-protocol field additions in upstream get re-exported automatically (identity-preserving), but any behavioral changes need a passthrough audit.

## Development

```bash
git clone https://github.com/open-gitagent/computer-agent-py
cd computer-agent-py
uv sync --all-extras --dev
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest -q                    # 120+ unit tests
uv run pytest -q -m integration     # requires ANTHROPIC_API_KEY + claude CLI
uv build
```

## License

MIT — see [`LICENSE`](LICENSE).
