# computer-agent-py

[![PyPI](https://img.shields.io/pypi/v/computer-agent-py.svg)](https://pypi.org/project/computer-agent-py/)
[![Python](https://img.shields.io/pypi/pyversions/computer-agent-py.svg)](https://pypi.org/project/computer-agent-py/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Drop-in replacement for [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/)** — change the import line, get a proxied telemetry pipeline with PII redaction, configurable OTel export, policy-based tool authorization, and full AgentOS integration for free.

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
pip install 'computer-agent-py[all]'              # everything
```

**Prerequisite** — same as upstream: the `claude` CLI binary on `PATH` and Anthropic / Bedrock credentials in the environment.

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
| `AGENTOS_MONGO_URL` | When set + `[agentos]` installed, attaches AgentOS sinks (registry, logs, sessions, agent_messages, slack_threads). |
| `AGENTOS_MONGO_DB` | Mongo database name. Default: `agentos`. |
| `COMPUTERAGENT_CAPTURE_CONTENT` | `1` to include prompts/responses on OTel spans. Default: off. |
| `COMPUTERAGENT_CAPTURE_CONTENT_MODE` | `events` (default) \| `attributes` \| `both`. |

### Programmatic

```python
from computeragent import configure, PiiRedactor, GuardrailFilter
from computeragent.telemetry.sinks import OtelSink, AgentRegistrySink, MongoMessageSink

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
        AgentRegistrySink(mongo_url="mongodb://..."),                   # registry + logs + sessions + slack_threads
        MongoMessageSink(mongo_url="mongodb://..."),                    # per-message archive
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

When `[agentos]` is installed and `AGENTOS_MONGO_URL` is set, every agent run writes to the Mongo collections the AgentOS frontend already reads:

| Collection | Per | What's in it |
|---|---|---|
| `agent_registry` | agent name | Identity + harness + model + last-seen; idempotent upsert |
| `agent_logs` | run | Rolled-up query/reply + tokens + cost + ok/error — drives the Logs tab |
| `sessions` | session | `entries[]` of `{type, text}` chat-bubble messages — drives the Chat tab transcript |
| `slack_threads` | session | TS parity row that drives the per-agent `sessionCount` + `lastActivity` aggregates |
| `agent_messages` | message | Per-event archive (`user_message`, `assistant_message`, `tool_use`, `tool_result`, `usage_snapshot`, `policy_decision`, `system_message`) for replay, RAG, audit |

The doc shapes are byte-for-byte compatible with the TypeScript `@open-gitagent/agent-registry-mongo` package — Python-driven agents show up in the same AgentOS UI that hosted TS agents do, with no frontend change.

### Live chat for library-mode agents

When the AgentRegistrySink writes its `agent_registry.source` row, it includes a full inline `IdentitySource` with `files: {agent.yaml, CLAUDE.md}` derived from your `ClaudeAgentOptions`. If a user clicks "New Chat" on the agent in the AgentOS SPA, the harness can clone those files into a sandbox workdir and spawn a live conversation — same UX as hosted (git-sourced) agents. The historical transcript stays available in the Chat tab regardless.

### DocumentDB compatibility

Prod deployments on AWS DocumentDB work without changes. The sinks use only operators DocumentDB supports — `$set`, `$setOnInsert`, `$push`, `update_one(upsert=True)`, `insert_one`. No aggregation pipelines, transactions, change streams, or TTL indexes. Set `MONGO_URL` with the standard `tls=true&tlsCAFile=...` params and mount the DocumentDB CA bundle.

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
     ▼            ▼                       ▼             ▼
  OtelSink   AgentRegistrySink     MongoMessageSink  <user>
     │            │                       │
     ▼            ▼                       ▼
  OTLP backend  agent_registry      agent_messages
  (NR / DD /    agent_logs          (per-message
   ClickHouse / sessions             archive)
   Tempo …)     slack_threads
                (drives AgentOS UI)
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
| [`examples/with_agentos.py`](examples/with_agentos.py) | AgentOS Mongo writes |
| [`examples/with_message_archive.py`](examples/with_message_archive.py) | Per-message archive |
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
