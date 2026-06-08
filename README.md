# computer-agent-py

[![PyPI](https://img.shields.io/pypi/v/computer-agent-py.svg)](https://pypi.org/project/computer-agent-py/)
[![Python](https://img.shields.io/pypi/pyversions/computer-agent-py.svg)](https://pypi.org/project/computer-agent-py/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Drop-in replacement for [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/)** — change one import line, keep every call site identical, and gain a proxied telemetry pipeline with PII redaction, configurable OTel export, policy-based tool authorization, and full AgentOS integration for free.

**New in 0.2.0** — a Python port of the TypeScript [`ComputerAgent`](../ComputerAgent/) harness alongside the drop-in proxy. Same vocabulary (engine / substrate / session-store / identity-loader), `ComputerAgent` + `run_task()` entry points, two built-in engines (`claude-agent-sdk`, `gitagent`). The 0.1.x drop-in surface is untouched — both modes co-exist.

**New in 0.2.1** — run the engine on a **remote harness server** (`ComputerAgent(harness_url=…)`); **AgentOS API-key auth** (`cak_` key via `harness_token` / `COMPUTERAGENT_HARNESS_TOKEN`); a **stable, rename-safe `agent_id`** (plus run a registered agent by reference, no `source`); **private GAP-repo git credentials** resolved per-group from AgentOS; and the `gap` identity loader. **Breaking:** AgentOS telemetry now ships over **HTTP** to the server's ingest endpoint — the old direct-Mongo sinks (`AGENTOS_MONGO_URL`) are gone. See [`CHANGELOG.md`](CHANGELOG.md).

> **Package vs import name** — PyPI distribution is `computer-agent-py` (hyphens for the wheel); the import name is `computeragent` (Python doesn't allow hyphens). So `pip install computer-agent-py` then `from computeragent import …`.

## Why use this

Adopting `computer-agent-py` in place of `claude-agent-sdk` gives you, without rewriting your agent code:

- **OpenTelemetry traces** following the GenAI Semantic Conventions, vendor-neutral (New Relic, Datadog, ClickHouse, Honeycomb, Tempo, Jaeger — one env-var change).
- **PII redaction** at the package boundary — email, phone, SSN, credit cards, AWS keys redacted before anything reaches a sink.
- **Generic guardrails** — attribute truncation, tool allowlists, per-session cost ceilings, content filters.
- **Policy-based tool-use authorization** — gate every tool call through OPA (remote) or Cedar (in-process). Fail-closed by default.
- **AgentOS visibility** — POST telemetry to the AgentOS ingest endpoint; the server projects it into the collections the frontend reads (`agent_registry`, `agent_logs`, `sessions`, `chat_sessions`, `agent_messages`). The SDK holds no Mongo creds. Library-mode agents show up in the Agents list, Logs tab, and Chat transcript.
- **Per-message archive** — every message, tool call, and policy decision archived to `agent_messages` for replay, RAG, and forensic audit.

## Install

```bash
pip install computer-agent-py                     # core drop-in + OPA policy engine
pip install 'computer-agent-py[otel]'             # + OpenTelemetry sink
pip install 'computer-agent-py[agentos]'          # + AgentOS HTTP telemetry sink (ingest) + git-cred/agent resolve
pip install 'computer-agent-py[remote]'           # + remote-harness client (ComputerAgent(harness_url=…))
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

Built-ins:

| Axis | Built-ins | Notes |
|---|---|---|
| `engine` | `claude-agent-sdk`, `gitagent` | gitagent reads `GITCLAW_MODEL_BASE_URL` + `OPENAI_API_KEY`. MCP-only tools. |
| `runtime` (substrate) | `local` | Caller-owned `Path`, or git-clone an `https://` / `git@` `.git` URL into a temp dir. Remote substrates run server-side — see Mode 2b. |
| `session_store` | `memory` | In-process dict. Mongo + SQLite stores land in 0.3.0 (with resumability). |
| `identity_loader` | `passthrough`, `gap` | `passthrough` treats inline dicts as manifests; `gap` reads `agent.yaml` + `SOUL.md` + `RULES.md` (the `[gap]`/`[gitagent]` extra). |

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
    harness_token="cak_…",               # AgentOS API key — required
    harness="claude-agent-sdk",
    source={"type": "git", "url": "github.com/org/agent"},
) as agent:
    result = await agent.chat("Summarize the README.")

# ComputerAgent server / CAS (:8787) — full sandbox API, substrate chosen per request.
async with ComputerAgent(
    harness_url="http://cas:8787",
    harness_kind="server",
    harness_token="cak_…",               # or set COMPUTERAGENT_HARNESS_TOKEN
    runtime="bwrap",
    harness="claude-agent-sdk",
    source={"type": "git", "url": "github.com/org/agent"},
) as agent:
    result = await agent.chat("…")
```

| `harness_kind` | Server | Substrate | Permissions |
|---|---|---|---|
| `"protocol"` (default) | `/v1/sessions/*` (harness-server, :7700) | server-side | `on_tool_call` round-trip |
| `"server"` | `/sandboxes` + `/run` (CAS, :8787) | client-chosen via `runtime` | server-side `permission_mode`/SRS |
| `"auto"` | probes `/v1/health` then `/health` | — | — |

The protocol layer is also usable directly: `from computeragent import HarnessClient, ComputerAgentServerClient`. `run_task(harness_url=...)` works the same for one-shots.

**Authentication.** The remote harness / CAS validate an AgentOS API key (`cak_…`).
Pass it as `harness_token="cak_…"` (sent as `Authorization: Bearer`), or set
`COMPUTERAGENT_HARNESS_TOKEN` and omit the arg — explicit wins, env is the
fallback. The **same key works everywhere**: remote auth, telemetry ingest, and
the private-repo credential resolve below.

**No `ANTHROPIC_API_KEY` on the client — two paths:**

| Path | When to use | What to set |
|---|---|---|
| **Remote harness** (`harness_url=`) | Engine runs server-side | No Anthropic key on the client — the harness server holds it (`ANTHROPIC_API_KEY` in the server's env) |
| **AgentOS messages gateway** | Engine runs in-process | `ANTHROPIC_BASE_URL=https://<host>/agentos/api` + `ANTHROPIC_AUTH_TOKEN=cak_…` — the server validates the `cak_` and forwards with its own key |

```bash
# Remote harness — client needs zero Anthropic credentials:
COMPUTERAGENT_HARNESS_URL=https://cas.example.com
COMPUTERAGENT_HARNESS_TOKEN=cak_…
# (no ANTHROPIC_API_KEY)

# In-process + AgentOS gateway — also no raw Anthropic key on the client:
ANTHROPIC_BASE_URL=https://agentos.example.com/agentos/api
ANTHROPIC_AUTH_TOKEN=cak_…
AGENTOS_DISCOVERY_URL=https://agentos.example.com/agentos/api/discovery
COMPUTERAGENT_HARNESS_TOKEN=cak_…
```

**Stable agent identity.** Pass `agent_id="my-stable-slug"` (or set
`COMPUTERAGENT_AGENT_ID`) to give the agent a stable identifier that's decoupled
from `agent_name` (the display label). AgentOS keys the registry + per-agent
sessions/logs on it, so **renaming the agent doesn't re-register it** or strand
its history. Omit it and AgentOS falls back to keying on `agent_name` (legacy
behavior). Works in both in-process and remote modes.

**Run a registered agent by reference.** Once an agent exists in AgentOS, pass
**only** `agent_id` (no `source`) and the SDK resolves its `source`/`harness`/
`model` from the server (`POST {AGENTOS_API_URL}/agents/resolve`, authed with your
`cak_` key, scoped to the key's group):

```python
# Define + register on first run (source given):
async with ComputerAgent(agent_id="my-agent", source="github.com/org/agent") as a:
    await a.chat("…")
# Thereafter, run it by reference — no source needed:
async with ComputerAgent(agent_id="my-agent") as a:   # source fetched from AgentOS
    await a.chat("…")
```

Explicit `source`/`harness`/`model` override the resolved values; an unregistered
`agent_id` with no `source` raises a clear error (it's strictly "run an existing
agent").

### One config: `AGENTOS_DISCOVERY_URL` + your `cak_` key

You give the SDK **two things** and it figures out the rest:

| Env (SDK) | What |
|---|---|
| `AGENTOS_DISCOVERY_URL` | the full URL of the AgentOS **discovery** document, e.g. `https://host/agentos/api/discovery` |
| `COMPUTERAGENT_HARNESS_TOKEN` | the single `cak_` key the SDK presents everywhere |

The SDK GETs the discovery document once and reads the **absolute URLs** for
ingest, `agent_id`/git-cred resolve, and the OTLP trace gateway straight out of
it — it never hardcodes or constructs an endpoint path. If the server ever moves
a route, it updates its discovery document and the SDK picks the new URLs up on
the next start — **no SDK upgrade needed**. (The discovery document is public —
it carries only URLs, no secrets — exactly like OIDC's `.well-known`.)

The one `cak_` key authenticates all three resolved endpoints. For OTLP traces in
particular, `OtelSink` exports **through the AgentOS server**, which validates the
key and forwards to the real backend (New Relic / a Collector) with a
*server-held* credential — so you set no `OTEL_EXPORTER_OTLP_HEADERS` and ship no
vendor license key. The remote-harness URL (`harness_url=` /
`COMPUTERAGENT_HARNESS_URL`) is the one URL *not* discovered — the harness/CAS is
a separate service, always a full URL you supply.

**Advanced overrides.** Each endpoint also honours a full-URL override env var
that wins over discovery: `AGENTOS_INGEST_URL`, `AGENTOS_API_URL`,
`AGENTOS_OTEL_URL`. Set one to pin a single endpoint (or to run without discovery
entirely). Omit `AGENTOS_OTEL_URL`/discovery's otel and `OtelSink` exports OTLP
directly to a backend the classic way via `OTEL_EXPORTER_OTLP_ENDPOINT`/`_HEADERS`.

**No lost events.** Because an `agent_id` run is AgentOS-backed, ingest is
**required**: `ComputerAgent` raises at construction if `agent_id` is set but no
ingest URL resolves (from `AGENTOS_DISCOVERY_URL` or an `AGENTOS_INGEST_URL`
override) and no explicit `telemetry_pipeline` is configured — so a run's
telemetry is never silently dropped. (Runs without `agent_id` are unaffected.)
`agent_id` itself rides telemetry only and never touches the harness wire body.

### Mode 2c — private GAP repos (git credentials)

When `source` is a **private** git URL, the SDK fetches a short-lived,
group-scoped PAT from AgentOS so the clone succeeds — and the token never
touches `argv`, the repo URL, or the logs.

```python
async with ComputerAgent(
    source="github.com/org/private-agent",
    harness_token="cak_…",                 # or COMPUTERAGENT_HARNESS_TOKEN
) as agent:                                # + AGENTOS_DISCOVERY_URL → resolves the PAT
    result = await agent.chat("…")
```

- The SDK POSTs the repo URL + its `cak_` key to `POST {AGENTOS_API_URL}/git-credentials/resolve`; AgentOS returns the decrypted PAT for the key's **group** + the repo **host** (strictly group-scoped — no admin bypass).
- The token is injected via `GIT_CONFIG_*` → `http.<host>.extraHeader`, so it never appears in process args, the URL, or logs (host-only logging). `git@`/`ssh://` URLs pass through untouched.
- **Best-effort:** missing config, a miss (404), or an auth failure (401/403) falls back to an unauthenticated clone — public repos are unaffected.
- **SHA sync:** after cloning, the SDK captures `git rev-parse HEAD` and rides it on `session_started` as `agent_sha`; the AgentOS server stamps `sourceSha`/`sourceSyncedAt` on the registry doc.

Requires `AGENTOS_DISCOVERY_URL` (the SDK reads the api endpoint from it; or set `AGENTOS_API_URL` to override) + a `cak_` key whose role includes `git-credentials:read`. Uses `httpx` (the `[remote]`/`[agentos]` extra).

### Mode 2d — Anthropic messages gateway (no client API key)

AgentOS ships a transparent Anthropic-API reverse proxy (`POST /agentos/api/v1/messages`). Point `ANTHROPIC_BASE_URL` at it and use your `cak_` key as the auth token — the client holds **no raw Anthropic key**. The server validates the `cak_` key (checks `completion:run` permission) and forwards to Anthropic with its own server-held credential.

```bash
# .env — set once, every ComputerAgent run in this process uses the gateway
ANTHROPIC_BASE_URL=https://agentos.example.com/agentos/api
ANTHROPIC_AUTH_TOKEN=cak_…          # same key as COMPUTERAGENT_HARNESS_TOKEN
AGENTOS_DISCOVERY_URL=https://agentos.example.com/agentos/api/discovery
COMPUTERAGENT_HARNESS_TOKEN=cak_…
# ANTHROPIC_API_KEY — intentionally absent
```

```python
from computeragent import ComputerAgent

# No ANTHROPIC_API_KEY in env — the gateway handles it.
async with ComputerAgent(
    source={"type": "inline",
            "manifest": {"name": "my-agent", "version": "0.1.0"},
            "files": {"CLAUDE.md": "You are terse.",
                      "agent.yaml": "spec_version: '0.1.0'\nname: my-agent\nversion: 0.1.0\nmodel:\n  preferred: claude-haiku-4-5\nruntime:\n  permission_mode: bypassPermissions\n  max_turns: 2\n"}},
    engine="claude-agent-sdk",
    runtime="local",
    options={"model": "claude-haiku-4-5", "permission_mode": "bypassPermissions", "max_turns": 2},
) as agent:
    result = await agent.chat("Say hello.")
    print(result.messages)
```

The Anthropic client library reads `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` automatically — no code change beyond the env vars. Works with any `claude-agent-sdk` engine run (local or remote harness).

### Mode 2e — run an agent by MongoDB `_id`

When an agent is registered via the AgentOS UI or `POST /agents/register`, the API returns a MongoDB `_id` (24-hex string). Pass that directly as `agent_id` — the resolve endpoint now accepts both the sparse stable `agentId` field **and** the registry `_id`, so you don't need a prior SDK run to stamp the field:

```python
# Agent was registered in the AgentOS UI → got back _id = "6a243b029cd2a68df349da77"
async with ComputerAgent(
    agent_id="6a243b029cd2a68df349da77",   # registry _id OR stable agentId
    harness_url="https://cas.example.com",
    harness_kind="server",
    harness_token="cak_…",
) as agent:
    result = await agent.chat("Explain the architecture.")
```

No `source` needed — the SDK calls `POST {AGENTOS_API_URL}/agents/resolve`, the server looks up by `agentId` field first and falls back to `_id` if the field is absent (covers UI-registered agents that have never been run with `agent_id=`).

## End-to-end cookbook

The four patterns below map directly to the smoke test [`scripts/10_local_harness_gap.py`](scripts/10_local_harness_gap.py). Run them together to validate your AgentOS deployment end-to-end.

**Prerequisites** — set these in your environment (or a `.env` file):

```bash
AGENTOS_DISCOVERY_URL=https://<agentos-host>/agentos/api/discovery
COMPUTERAGENT_HARNESS_TOKEN=cak_…
COMPUTERAGENT_HARNESS_URL=https://<cas-host>          # for B
SMOKE_AGENT_ID=6a243b029cd2a68df349da77               # for B — registry _id or stable agentId
SMOKE_PRIVATE_REPO_URL=https://github.com/org/repo    # for C + D
```

### A — local run, model calls through the AgentOS gateway

```python
import os, asyncio
from computeragent import ComputerAgent
from computeragent.telemetry import Pipeline
from computeragent.telemetry.sinks import AgentOSHttpSink

# Route Anthropic calls through AgentOS — client holds no API key.
os.environ["ANTHROPIC_BASE_URL"] = "https://<agentos-host>/agentos/api"
os.environ["ANTHROPIC_AUTH_TOKEN"] = "cak_…"

ingest_url = "https://<agentos-host>/agentos/api/ingest/events"
pipeline = Pipeline(sinks=[AgentOSHttpSink(url=ingest_url, token="cak_…")])

inline = {
    "type": "inline",
    "manifest": {"name": "smoke-a", "version": "0.1.0"},
    "files": {
        "CLAUDE.md": "You are a general assistant.",
        "agent.yaml": "spec_version: '0.1.0'\nname: smoke-a\nversion: 0.1.0\nmodel:\n  preferred: claude-haiku-4-5\nruntime:\n  permission_mode: bypassPermissions\n  max_turns: 2\n",
    },
}

async def run():
    async with ComputerAgent(
        source=inline, engine="claude-agent-sdk", runtime="local",
        options={"model": "claude-haiku-4-5", "permission_mode": "bypassPermissions", "max_turns": 2},
        telemetry_pipeline=pipeline,
    ) as agent:
        result = await agent.chat("Say hello.")
    print(result.messages)

asyncio.run(run())
```

### B — run a registered agent by `agent_id`, no source

```python
import asyncio
from computeragent import ComputerAgent

async def run():
    async with ComputerAgent(
        agent_id="6a243b029cd2a68df349da77",   # registry _id returned by /agents/register
        harness_url="https://cas.example.com",
        harness_kind="server",
        harness_token="cak_…",
    ) as agent:
        result = await agent.chat("Explain the Lyzr Cognis architecture.")
    print(result.usage)

asyncio.run(run())
```

### C — verify a private-repo PAT is stored and decryptable

```python
import asyncio
from computeragent.harness.git_credential_client import resolve_git_credential

async def check():
    cred = await resolve_git_credential("https://github.com/org/private-agent")
    if cred:
        tok = cred["token"]
        print(f"PAT OK: {tok[:4]}…{tok[-4:]}")  # never log the full token
    else:
        print("PAT not found — add it in AgentOS Settings → Git Credentials")

asyncio.run(check())
```

### D — full private-repo clone + agent run

The coordinator auto-resolves the PAT (via `POST {AGENTOS_API_URL}/git-credentials/resolve`) and injects it as an HTTP auth header via `GIT_CONFIG_*` — the token never touches `argv` or the clone URL:

```python
import asyncio, os
from computeragent import ComputerAgent

# Ensure we run locally, not against the remote CAS.
os.environ.pop("COMPUTERAGENT_HARNESS_URL", None)

async def run():
    async with ComputerAgent(
        source="https://github.com/org/private-agent",
        engine="claude-agent-sdk",
        runtime="local",
        options={"model": "claude-haiku-4-5", "permission_mode": "bypassPermissions", "max_turns": 2},
    ) as agent:
        result = await agent.chat("Summarize the README.")
    print(result.messages)

asyncio.run(run())
```

Requirements: `AGENTOS_DISCOVERY_URL` + `COMPUTERAGENT_HARNESS_TOKEN` with `git-credentials:read` role. A PAT for the repo's host must be stored in AgentOS Settings → Git Credentials under the key's group. Public repos work without any credential stored.

## Drop-in replacement for `claude-agent-sdk`

```diff
- from claude_agent_sdk import ClaudeAgentOptions, query
- from claude_agent_sdk.types import ResultMessage
+ from computeragent import ClaudeAgentOptions, query
+ from computeragent.types import ResultMessage
```

Every other line stays identical. `isinstance(msg, ResultMessage)` still works. The `claude` CLI subprocess, AWS Bedrock auth, MCP servers, permission modes, `cwd`, `add_dirs` — all behave exactly as the upstream SDK does. The `claude-agent-sdk` binary is bundled — no separate install needed.

```python
from computeragent import ClaudeAgentOptions, query
from computeragent.types import ResultMessage

options = ClaudeAgentOptions(
    model="claude-haiku-4-5",
    system_prompt="You are a helpful assistant.",
    allowed_tools=["Read", "Glob", "Grep"],
    permission_mode="bypassPermissions",
)

async for message in query(prompt="Summarize the README.md.", options=options):
    if isinstance(message, ResultMessage):
        print(f"answer ({message.num_turns} turns): {message.result}")
```

You gain OTel traces, PII redaction, policy-gated tool calls, and AgentOS visibility for free — with zero changes beyond the import line.

## Quickstart

See the [Drop-in replacement](#drop-in-replacement-for-claude-agent-sdk) section for the one-import-line quick start, or the [End-to-end cookbook](#end-to-end-cookbook) for the full AgentOS-integrated flow. Telemetry is configured from env vars — without any set, nothing leaves your process.

## Configure telemetry

### Env-driven (zero code change)

| Variable | Effect |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Base URL of an OTLP/HTTP backend. The sink appends `/v1/traces` and `/v1/metrics` automatically — pass the base. Unset → console exporter (debug). |
| `OTEL_EXPORTER_OTLP_HEADERS` | Comma-separated `key=value` (e.g. `api-key=NRRX-...` for New Relic). |
| `OTEL_SERVICE_NAME` | `service.name` attribute on every span. Default: `computeragent`. |
| `COMPUTERAGENT_OTEL` | `disabled` to suppress OtelSink. |
| `COMPUTERAGENT_OTEL_TEMPORALITY` | `delta` (default) or `cumulative`. SaaS OTLP intakes (New Relic, Datadog direct, Honeycomb) require delta; cumulative is only correct when exporting to a collector that aggregates downstream. |
| `AGENTOS_DISCOVERY_URL` | The full URL of the AgentOS discovery document (e.g. `https://host:8788/agentos/api/discovery`). With `[agentos]` installed + a `cak_` key, the SDK reads the ingest endpoint from it and attaches `AgentOSHttpSink` — POSTing telemetry events to the AgentOS server, which writes the Mongo collections (registry, logs, sessions, chat_sessions, agent_messages). The single config for *all* AgentOS endpoints. |
| `AGENTOS_INGEST_URL` | Full-URL **override** for the ingest endpoint (wins over discovery). E.g. `http://host:8788/agentos/api/ingest/events`. |
| `COMPUTERAGENT_HARNESS_TOKEN` | The `cak_` key, sent as `Authorization: Bearer …` to ingest + every resolved AgentOS endpoint. (Legacy `AGENTOS_INGEST_TOKEN` is still honoured.) |
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
            tool_name_allowlist={"Read", "Glob", "Grep", "mcp__nordassist-tools__*"},
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
    resource_resolver=lambda ctx: PolicyResource(agent_name="nordassist", model="claude-sonnet-4-5"),
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
