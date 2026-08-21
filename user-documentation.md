# computer-agent-py — User Guide

A practical guide for **using** the SDK: installing it, connecting it to your **ComputerAgent Console**, running agents locally, seeing traces and enforcing guardrails from the console, and using the **process-forking** system.

> **Package name vs import name:** you `pip install computer-agent-py` but `import computeragent`.

- [1. Install](#1-install)
- [2. 60-second quickstart](#2-60-second-quickstart)
- [3. Connect to the ComputerAgent Console (URLs & keys)](#3-connect-to-the-computeragent-console-urls--keys)
- [4. Model credentials](#4-model-credentials)
- [5. Running agents](#5-running-agents)
- [6. Guardrails & policies from the console](#6-guardrails--policies-from-the-console)
- [7. Process forking — spawn sub-agents as separate processes](#7-process-forking--spawn-sub-agents-as-separate-processes)
- [8. Full environment variable reference](#8-full-environment-variable-reference)
- [9. Troubleshooting](#9-troubleshooting)

---

## 1. Install

```bash
pip install 'computer-agent-py[all]'      # everything (recommended to start)
```

Slimmer installs if you know what you need:

```bash
pip install computer-agent-py             # core (drop-in + OPA policy)
pip install 'computer-agent-py[agentos]'  # + console (ingest / git-creds / resolve)  ← needed to connect to the console
pip install 'computer-agent-py[otel]'     # + OpenTelemetry export
pip install 'computer-agent-py[gap]'      # + GAP identity loader (agent-as-git-repo)
pip install 'computer-agent-py[cedar]'    # + in-process Cedar policy engine
pip install 'computer-agent-py[remote]'   # + remote-harness client
```

**Prerequisite:** the `claude` CLI ships automatically with the dependency `claude-agent-sdk` — nothing extra to install. You need an Anthropic (or Bedrock) key (see [§4](#4-model-credentials)).

---

## 2. 60-second quickstart

Run Claude Code locally, in-process, with telemetry going to your console:

```python
import asyncio
from computeragent import ComputerAgent

async def main():
    async with ComputerAgent(
        source="github.com/your-org/your-gap-agent",   # a GAP repo, OR a local path, OR an inline dict
        identity_loader="gap",
        agent_id="my-first-agent",                     # stable id shown in the console
    ) as agent:
        result = await agent.chat("List the files in this repo and summarize the README.")
        print(result.messages)
        print("tokens:", result.usage.input_tokens, result.usage.output_tokens, "cost:", result.usage.cost_usd)

asyncio.run(main())
```

With the environment from [§3](#3-connect-to-the-computeragent-console-urls--keys) set, this run shows up in your console: a session, a transcript, token/cost, and (if OTel is on) a trace.

---

## 3. Connect to the ComputerAgent Console (URLs & keys)

**You only need TWO things.** The SDK asks the console where everything lives — you never hand-configure the ingest/trace/api URLs individually.

### 3.1 The two required settings

| Setting | Env var | What to put |
|---|---|---|
| **Discovery URL** | `AGENTOS_DISCOVERY_URL` | `https://<your-console-host>/agentos/api/discovery` |
| **API key** | `COMPUTERAGENT_HARNESS_TOKEN` | a `cak_…` key you mint in the console (see §3.3) |

```bash
export AGENTOS_DISCOVERY_URL="https://<your-console-host>/agentos/api/discovery"
export COMPUTERAGENT_HARNESS_TOKEN="cak_xxxxxxxxxxxxxxxxxxxxxxxxx"
```

**Concrete example** (the shared test console):

```bash
export AGENTOS_DISCOVERY_URL="https://agentos.test.studio.lyzr.ai/agentos/api/discovery"
export COMPUTERAGENT_HARNESS_TOKEN="cak_…"
```

### 3.2 How discovery works (what those two settings buy you)

The discovery URL is **public** and returns the real, absolute endpoints:

```jsonc
GET https://<your-console-host>/agentos/api/discovery
{
  "version": "…",
  "endpoints": {
    "ingest":   "https://<host>/agentos/api/ingest/events",   // telemetry (sessions, transcripts, usage)
    "api":      "https://<host>/agentos/api/v1",               // dashboard/registry API + guardrail proxy
    "otel":     "https://<host>/agentos/api/otel",             // OTLP trace gateway
    "messages": "https://<host>/agentos/api/v1/messages"
  }
}
```

The SDK fetches this once and routes everything for you. **Set only `AGENTOS_DISCOVERY_URL` — not the individual endpoints.** The `cak_` key is presented as `Authorization: Bearer` on every call.

### 3.3 Minting the `cak_` key

In the console: **Settings → API Keys → Create a key**. Give it a label, pick a **group** (scopes what the key can see) and a **role** (`agentos-editor` is a good default), then **Create key**. The plaintext `cak_…` is shown **once** — copy it into `COMPUTERAGENT_HARNESS_TOKEN`.

> Keep the key secret (env var or a secrets manager — never in code). Revoke it from the same page when you're done.

### 3.4 Optional / advanced URL settings

You normally never set these — they only override discovery for edge cases.

| Env var | Purpose |
|---|---|
| `AGENTOS_INGEST_URL` | Pin the telemetry ingest endpoint (full URL) instead of using discovery. |
| `AGENTOS_API_URL` | Pin the dashboard/api endpoint. |
| `AGENTOS_OTEL_URL` | Pin the OTLP gateway endpoint (turns on console-routed traces). |
| `COMPUTERAGENT_HARNESS_URL` | **Separate service** — run the engine on a *remote* harness server (not the observability console). Not discovered; always a full URL. |

### 3.5 Traces to the console vs. straight to your vendor

- **Through the console gateway (recommended):** set `AGENTOS_OTEL_URL` (or let discovery provide `otel`). The SDK sends OTLP with your `cak_`; the console forwards to the trace backend. No vendor keys on your machine.
- **Direct to your own OTel backend:** set the standard `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` / `OTEL_SERVICE_NAME`.
- **Off:** `export COMPUTERAGENT_OTEL=disabled`.

### 3.6 What lands in the console

With the two settings in place, every run reports: the **agent** (registry), the **session transcript** (every user/assistant turn + tool calls), **token usage & cost**, **traces** (if OTel is on), and honors any **guardrail policy** you bind to the agent ([§6](#6-guardrails--policies-from-the-console)).

---

## 4. Model credentials

The `claude-agent-sdk` engine (default) needs Anthropic access:

```bash
export ANTHROPIC_API_KEY="sk-ant-…"
# Bedrock instead: pass AWS creds + a Bedrock model id via envs=/claude_env; optionally ANTHROPIC_BASE_URL for a gateway.
```

Using the `gitagent` engine (OpenAI-compatible endpoints) instead:

```bash
export GITCLAW_MODEL_BASE_URL="https://your-openai-compatible-endpoint/v1"
export OPENAI_API_KEY="…"
# anthropic:-prefixed models route to ANTHROPIC_API_KEY (+ optional ANTHROPIC_BASE_URL)
```

---

## 5. Running agents

### 5.1 Sources — three ways to point at an agent

```python
ComputerAgent(source="github.com/org/agent", identity_loader="gap")   # a GAP git repo (cloned; private repos auto-auth via the console)
ComputerAgent(source="./local/gap-agent",    identity_loader="gap")   # a local GAP repo
ComputerAgent(source={"name": "inline", "options": {                  # an inline manifest (passthrough loader)
    "system_prompt": "You are a helpful assistant.", "model": "claude-haiku-4-5",
}}, identity_loader="passthrough")
```

A **GAP repo** (`agent.yaml` + `SOUL.md`/`RULES.md`, optional `tools/`, `agents/`, `hooks/`, `skills/`) is the richest option — it defines the persona, tools, sub-agents, hooks, and skills declaratively.

### 5.2 Multi-turn — one conversation

One `ComputerAgent` instance is **one session**. Reuse it across turns:

```python
async with ComputerAgent(source="./agent", identity_loader="gap", agent_id="assistant") as agent:
    a = await agent.chat("What's in config.yaml?")
    b = await agent.chat("Now change the port to 9000.")   # same session, remembers turn 1
    # both turns collapse into ONE console session
```

### 5.3 Run a registered agent by reference (no source)

If an agent is already registered in the console, run it by id — the SDK resolves its source/model for you:

```python
ComputerAgent(agent_id="my-registered-agent")   # no source= needed
```

### 5.4 Common options

```python
ComputerAgent(
    source="./agent", identity_loader="gap", agent_id="assistant",
    model="claude-haiku-4-5",
    options={"permission_mode": "bypassPermissions", "max_turns": 12},
    budget_max_usd=0.50,
)
```

---

## 6. Guardrails & policies from the console

Create an SRS/RAI policy in **Console → Policies**, bind it to your agent, and the SDK enforces it on the **local** run in real time — even under `permission_mode="bypassPermissions"` (enforcement rides a `PreToolUse` hook). Nothing to configure in code beyond your console connection; the SDK auto-wires SRS from the environment. Unbind (or switch to an allow policy) in the console and the change takes effect on the next run.

> Cedar policies are **default-deny**: a tool-targeted `forbid` needs a `permit(principal, action, resource);` baseline or every tool is denied.

You can also enforce policy fully in-process without the console using the OPA (`[core]`) or Cedar (`[cedar]`) engines — see `docs/ARCHITECTURE.md` §9.2.

---

## 7. Process forking — spawn sub-agents as separate processes

Normally a sub-agent (the Claude "Task" tool) runs **inside** your agent's process and shares its session, so you can't observe or guardrail it on its own. **Forking** turns a sub-agent into a **real separate `ComputerAgent` process** — its own Claude CLI, its own workspace, and its **own session / agent_id** in the console, linked back to the parent. Opt-in; **off by default**.

### 7.1 The three modes

| `fork_mode` | What happens |
|---|---|
| `"off"` (default) | Nothing changes — native in-process sub-agents work as before. |
| `"manual"` | Adds the `mcp__fork__spawn` tool; your agent forks a separate process when it decides to. Native sub-agents stay available too. |
| `"auto"` | Disables native sub-agents and steers **all** delegation to forks — every sub-agent becomes its own process. |

### 7.2 Enable it

```python
from computeragent import ComputerAgent, ForkConfig

async with ComputerAgent(
    source="./orchestrator-agent", identity_loader="gap", agent_id="orchestrator",
    options={"permission_mode": "bypassPermissions"},
    fork_mode="auto",                              # or "manual"
    fork_config=ForkConfig(
        max_depth=2,          # a child may fork again only while under this depth
        max_children=8,       # lifetime cap on how many children one agent spawns
        max_concurrency=4,    # how many children run at once
        child_timeout_s=300,  # per-child timeout
    ),
) as agent:
    await agent.chat(
        "Research three topics — the ocean, the moon, and volcanoes — "
        "delegate each to a separate specialist and summarize their findings."
    )
    # → three separate Claude Code processes run (in parallel), each its own console session.
```

### 7.3 The fork tool your agent calls

`mcp__fork__spawn` accepts:

| Arg | Meaning |
|---|---|
| `task` (required) | the subtask to hand the child |
| `agent` | a **preset** name from your repo's `agents/<name>` (materialized into a real child process) |
| `name` + `role` | define a **dynamic** child on the fly (its system prompt/persona) |
| `model`, `tools` | override the child's model / restrict its tools |

It returns the child's result text plus the child's `agent_id`, `session`, and cost — so the parent can reason about it.

### 7.4 What you get in the console

Each fork is a **first-class agent**: its own session and `agent_id` (formatted `parent/child#id`), with parent-linkage fields (`parent_session_id`, `parent_agent_id`) so children can be nested under their parent. Each child's tokens/cost and guardrail decisions are tracked independently.

### 7.5 Safety

Depth, fan-out, concurrency and timeout are all capped (`ForkConfig`). Children are cleaned up (temp workspace + subprocess) when the parent finishes or is cancelled. Forks can only run your own repo's presets or a dynamic persona — never an arbitrary external repo.

### 7.6 When to use which

- **manual** — you want an isolated, independently-observable worker occasionally (heavy or risky subtasks) while keeping normal in-process sub-agents.
- **auto** — you want *every* delegation to be its own traceable, independently-guardrailed process.
- **off** — you don't need process isolation; keep the lighter in-process sub-agents.

---

## 8. Full environment variable reference

| Variable | Required? | Purpose |
|---|---|---|
| `AGENTOS_DISCOVERY_URL` | to use the console | `https://<host>/agentos/api/discovery` — the one URL you set |
| `COMPUTERAGENT_HARNESS_TOKEN` | to use the console | your `cak_…` API key (Bearer everywhere) |
| `ANTHROPIC_API_KEY` | for the default engine | Anthropic auth (or use Bedrock via `envs=`) |
| `ANTHROPIC_BASE_URL` | optional | Anthropic gateway / regional endpoint |
| `GITCLAW_MODEL_BASE_URL` + `OPENAI_API_KEY` | for `gitagent` | OpenAI-compatible endpoint |
| `COMPUTERAGENT_AGENT_ID` | optional | default stable agent id |
| `COMPUTERAGENT_OTEL` | optional | set `disabled` to turn off trace export |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `_HEADERS` / `OTEL_SERVICE_NAME` | optional | export traces straight to your own OTel backend |
| `AGENTOS_INGEST_URL` / `AGENTOS_API_URL` / `AGENTOS_OTEL_URL` | optional | pin an individual console endpoint (override discovery) |
| `COMPUTERAGENT_HARNESS_URL` | optional | remote-harness server (separate service) |
| `SRS_BASE_URL` / `SRS_API_KEY` / `SRS_POLICY_ID` / `SRS_PRINCIPAL_ID` | optional | bind SRS guardrails without the console binding lookup |
| `COMPUTERAGENT_CAPTURE_CONTENT` / `_MODE` | optional | capture prompt/completion/tool content in traces (`attribute`/`event`/`both`) |

---

## 9. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Nothing appears in the console | `AGENTOS_DISCOVERY_URL` or `COMPUTERAGENT_HARNESS_TOKEN` unset, or the key was revoked. Verify the discovery URL loads in a browser. |
| `401` on ingest/api | Bad/expired `cak_` — mint a fresh one in Settings → API Keys. |
| `agent_id is set but no AgentOS ingest is configured` | You set `agent_id` but no discovery URL / pipeline — set `AGENTOS_DISCOVERY_URL` (so events aren't silently dropped). |
| Traces missing but sessions present | OTel off/misrouted — set `AGENTOS_OTEL_URL` (or the `OTEL_EXPORTER_*` trio), and ensure `COMPUTERAGENT_OTEL` isn't `disabled`. |
| Guardrail not enforced | Ensure the policy is **bound** to the agent in the console and your discovery/key are set (SRS auto-wires from them). |
| Your agent isn't visible in the console list | On some console builds, SDK-registered agents may be scoped by team ownership — check the team switcher / with an admin. |
| A private GAP repo won't clone | The console's GitHub App must be installed on that repo; your key's role needs `git-credentials:read`. |

---

*For internals — architecture, every module, the telemetry/policy design — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).*
