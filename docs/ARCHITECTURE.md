# computer-agent-py — Architecture & End-to-End Reference

> Complete reference for the `computer-agent-py` package (import name **`computeragent`**): what it is, every moving part, how the pieces fit, the full public API, the AgentOS control-plane integration, and the process-level agent-forking feature. This is the "everything, end to end" companion to the task-focused [`README.md`](../README.md).

- **PyPI distribution:** `computer-agent-py` · **import name:** `computeragent` (Python disallows hyphens)
- **Version:** `0.2.3` (this doc also covers the unreleased `feat/process-fork` work)
- **Runtime:** Python ≥ 3.10 · async-first (`asyncio`/`anyio`)
- **Upstream:** a superset of [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/) — every public symbol is re-exported identity-preserving, so `computeragent` is a drop-in replacement.

---

## 1. What this package is

`computer-agent-py` is two co-existing surfaces over the Anthropic Claude Agent SDK, sharing one telemetry + policy core:

1. **Drop-in proxy** (0.1.x, unchanged) — replace `from claude_agent_sdk import …` with `from computeragent import …` and every `query()` / `ClaudeSDKClient` call transparently flows through PII redaction → guardrails → OpenTelemetry + AgentOS sinks. Zero call-site changes.
2. **Harness** (`ComputerAgent`, 0.2.x) — a Python port of the TypeScript ComputerAgent harness. It composes an agent from **four orthogonal, swappable axes** and gives you a multi-turn chat surface, a remote-harness client, run-by-reference, and full OpenGAP (GitAgentProtocol) support.

Both surfaces emit the same `TelemetryEvent` stream and honor the same policy authorizer.

```
from computeragent import query, ClaudeAgentOptions        # Mode 1 — drop-in proxy
from computeragent import ComputerAgent, run_task          # Mode 2 — harness
```

---

## 2. Install

Core install is dependency-light (`claude-agent-sdk` + `typing-extensions`); everything else is an **extra**.

| Install | Adds | For |
|---|---|---|
| `computer-agent-py` | core drop-in + OPA policy engine | minimum-friction observability |
| `…[otel]` | OpenTelemetry API/SDK/OTLP exporter | ship GenAI spans/metrics out of process |
| `…[agentos]` | `httpx` | AgentOS HTTP ingest sink + git-cred / agent-resolve |
| `…[remote]` | `httpx` | remote-harness client (`ComputerAgent(harness_url=…)`) |
| `…[cedar]` | `cedarpy` | in-process Cedar policy engine |
| `…[gitagent]` | `openai`, `anthropic`, `pyyaml` | the `gitagent` engine + GAP loader |
| `…[gap]` | `pyyaml` | the GAP identity loader only |
| `…[sqlite]` | `aiosqlite` | SQLite-backed `SessionStore` implementations |
| `…[all]` | all of the above | everything |

**Prerequisite:** the bundled `claude` CLI ships transitively with `claude-agent-sdk` (a ~218 MB binary under `claude_agent_sdk/_bundled/claude`) — no separate install. You need Anthropic (or Bedrock) credentials in the environment.

---

## 3. Mental model — the four orthogonal axes

The harness composes an agent from four independent Protocols. Any axis can be swapped without touching the others; third parties register their own implementations.

```
                        ┌──────────────── ComputerAgent ────────────────┐
   client (.chat) ─────▶│                HarnessCoordinator             │
                        │                                                │
   EngineDriver ────────┤  claude-agent-sdk │ gitagent                   │  ← WHAT loop runs
   Substrate ───────────┤  local            │ (e2b/…) registerable       │  ← WHERE it runs
   IdentityLoader ──────┤  passthrough      │ gap                        │  ← WHO the agent is
   SessionStore ────────┤  memory           │ (Protocol; sqlite extra)   │  ← transcript persistence
                        └───────────────────┬────────────────────────────┘
                                            │  TelemetryEvent stream
                                            ▼
              middleware (PII redact, guardrails) → sinks (OTel, AgentOS ingest, content)
                                            │
                                            ▼
                                AgentOS control plane (observability + guardrails)
```

| Axis | Protocol | Built-ins | Registry |
|---|---|---|---|
| Engine (agent loop) | `EngineDriver` | `claude-agent-sdk`, `gitagent` | `register_engine(name, impl)` |
| Substrate (where it runs) | `Substrate` | `local` | `register_substrate(name, impl)` |
| Identity (who it is) | `IdentityLoader` | `passthrough`, `gap` | `register_identity_loader(name, impl)` |
| Session store (transcript) | `SessionStore` | `memory` | `register_session_store(name, impl)` |

All Protocols live in `computeragent.protocol_types`.

---

## 4. Public API

### 4.1 `ComputerAgent`

The top-level harness client (`computeragent.harness.ComputerAgent`). All parameters are keyword-only.

```python
from pathlib import Path
from computeragent import ComputerAgent

async with ComputerAgent(source=Path("./my-gap-agent"), identity_loader="gap") as agent:
    result = await agent.chat("Summarize the repo.")
    print(result.messages, result.usage, result.session_id)
```

| Parameter | Default | Purpose |
|---|---|---|
| `source` | — | Identity source: a local `Path`, a git URL (cloned), or an inline `dict` manifest. Required unless `agent_id` resolves one. |
| `engine` | `"claude-agent-sdk"` | Engine name or an `EngineDriver` instance. |
| `runtime` | `"local"` | Substrate name or a `Substrate` instance. |
| `identity_loader` | `"passthrough"` | Loader name or an `IdentityLoader` instance. |
| `session_store` | `None` | Store name or a `SessionStore` instance (transcript persistence + resume). |
| `session_id` | `None` | Override the minted session id. |
| `agent_name` / `agent_description` | `None` | Display label / description (telemetry + registry). |
| `agent_id` | env `COMPUTERAGENT_AGENT_ID` | Stable identifier decoupled from `agent_name` (rename-safe). Doubles as a **run-by-reference handle**: set it with no `source` → the agent's source/harness/model are resolved from AgentOS. |
| `model` | `None` | Model override (folds into options). |
| `options` | `None` | Extra engine options (merged over the loader's; caller wins). E.g. `{"permission_mode": "bypassPermissions", "max_turns": 8}`. |
| `envs` | `None` | Extra env dict injected into the engine subprocess. |
| `policy_decider` | env-autowired (SRS) | A `PolicyDecider` gating tool calls; auto-wired from SRS env when unset. |
| `on_tool_call` | `None` | Async permission callback for interactive approval. |
| `budget_max_usd` | `None` | Cost ceiling (plumbed to the engine context). |
| `telemetry_pipeline` | shared singleton | Inject a specific `Pipeline` (else the process-wide one). |
| `project_key` | `"default"` | Session-store partition key. |
| `harness_url` / `harness` / `harness_kind` / `harness_auth` / `harness_token` | `None` | **Remote-harness mode** — run the engine on a remote ComputerAgent server. |
| `policy_id` | `None` | Bind an SRS policy id directly (no AgentOS binding lookup). |
| `fork_mode` / `fork_config` | `"off"` / `None` | **Process-level forking** (§10). |

**Lifecycle:** one `ComputerAgent` instance == one logical session. `async with` (or `dispose()`) fires exactly one `session_started` (first chat) and one `session_ended` (dispose), so N chats collapse into one conversation. `.chat(msg)` returns a lazy `ChatHandle`.

### 4.2 `ChatHandle` / `ChatResult`

```python
handle = agent.chat("…")          # lazy — nothing runs yet
async for event in handle: ...    # stream engine events
result = await handle             # or drain to a ChatResult
```

- `ChatResult` (frozen): `session_id: str`, `messages: tuple[...]`, `usage: UsageRollup`, `duration_ms: float`.
- `UsageRollup`: `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `cost_usd`.
- `ChatHandle`: `__aiter__` (stream), `__await__` → `result()`, `get_usage()`, `cancel()`.

### 4.3 `run_task(...)`

One-shot convenience: `async with ComputerAgent(...) as a: return await a.chat(msg)`.

### 4.4 Drop-in proxy

`computeragent.query`, `ComputerAgent`-free — every `claude_agent_sdk` symbol (`query`, `ClaudeSDKClient`, `ClaudeAgentOptions`, message/content types, `create_sdk_mcp_server`, `tool`, …) is re-exported. `computeragent.X is claude_agent_sdk.X` for identity-preserving `isinstance` checks. Derivation helpers in `computeragent._proxy`.

---

## 5. Engines (`EngineDriver`)

An engine is the agent loop. It receives an `EngineContext` (manifest, workspace, envs, permission callback, cancel event) and yields `EngineEvent`s the coordinator translates into telemetry.

### `claude-agent-sdk` (default)
Wraps `claude_agent_sdk.query()`. Each run spawns its **own bundled `claude` CLI subprocess** (`--output-format stream-json --input-format stream-json`). Streams partial messages for TTFT. Sets `IS_SANDBOX=1`. Filters `options` to real `ClaudeAgentOptions` fields before construction. Auth via `ANTHROPIC_API_KEY` (or Bedrock `AWS_*` via `envs=`); optional `ANTHROPIC_BASE_URL` for a gateway.

### `gitagent`
A Python re-implementation of the gitclaw loop against an OpenAI-compatible endpoint, with Anthropic dispatch by model prefix (`openai:…` vs `anthropic:…`). Env: `GITCLAW_MODEL_BASE_URL` + `OPENAI_API_KEY` (OpenAI path); `ANTHROPIC_API_KEY` (+ optional `ANTHROPIC_BASE_URL`) for anthropic-prefixed models. Non-streaming (no TTFT). Has a real `max_turns` loop bound.

---

## 6. Substrates (`Substrate`)

Where the engine's workspace lives. `boot(source, envs, git_credential)` → a `SubstrateHandle(workspace_path, close, revision)`.

### `local` (built-in)
- **`Path` source** → used directly, caller-owned (never deleted).
- **git URL** → `git clone --depth 1` into a `mkdtemp` dir; `close` rmtrees it; `revision` = cloned SHA. Private GAP repos: the coordinator resolves a short-lived token from AgentOS and injects it via `GIT_CONFIG_*`/`extraHeader` (never in argv).
- **inline `dict`** → uses `Path.cwd()`.

Register cloud substrates (`e2b`, `vzvm`, …) via `register_substrate`.

---

## 7. Identity loaders (`IdentityLoader`)

`load(source, target_engine, workspace_path)` → an `AgentManifest` (name, version, options, sha, optional `harden`).

### `passthrough` (built-in)
Treats the source as the manifest itself. A `dict` source lifts `name`/`version`/`options`/`sha` straight through; a `Path`/`str` source yields `name = workspace.name`, `options = {}` (the engine uses its own defaults / a workspace `CLAUDE.md`).

### `gap` — GitAgentProtocol (agent-as-git-repo)
Reads a full GAP repo at the workspace root and stitches it into `ClaudeAgentOptions`. Repo layout:

| Path | Becomes |
|---|---|
| `agent.yaml` | name, version, `model:` (provider-prefixed), `runtime.max_turns`/`budget_usd`, `tools:` allowlist, `gateway:`, external `mcp_servers:` (HTTP, `${VAR}` substitution) |
| `SOUL.md` / `RULES.md` / `AGENTS.md` | appended to the `claude_code` preset system prompt |
| `tools/*.yaml` (+ scripts) | builtin-tool aliases, and **script tools → an in-process `gap_tools` MCP server** (`mcp__gap_tools__<name>`) via `create_sdk_mcp_server` |
| `agents/<name>.yaml` or `agents/<name>/{agent.yaml,SOUL.md}` | native in-process **sub-agents** → the SDK's `agents: dict[str, AgentDefinition]` (invoked via the Task/Agent tool) |
| `hooks/hooks.yaml` | `PreToolUse`/`PostToolUse`/… hooks |
| `skills/` | mirrored to `.claude/skills/` for CLI discovery |
| `compliance.supervision.human_in_the_loop` | a **`harden`** hook: forces non-prompting permission modes back to `default` (applied *after* the caller overlay — strictest wins) |

---

## 8. Session stores (`SessionStore`)

Transcript persistence and multi-process resume. Built-in: **`memory`** (`InMemorySessionStore`). `SessionStore` is a Protocol — implement your own (Mongo, SQLite via the `[sqlite]` extra, …) and `register_session_store`. When a store is set, prior turns are pre-loaded into engine context.

---

## 9. Telemetry, policy & AgentOS integration

### 9.1 Pipeline

`configure(sinks=…)` builds a process-wide `Pipeline`; `get_pipeline()` lazy-builds from env. Each `emit(event)` runs the **middleware chain** then fans out to **sinks** as tracked background tasks; `flush()` awaits them. The `HarnessCoordinator` owns the hot path (engines never emit directly). Every event is stamped with `agent_id` (+ fork `parent_*`, §10) at a single chokepoint.

**Middleware:** `PiiRedactor` (email/phone/SSN/cards/AWS keys redacted at the boundary), `GuardrailFilter` (attribute truncation, tool allowlists, per-session cost ceilings, content filters).

**Sinks:**
| Sink | Extra | Emits to |
|---|---|---|
| `OtelSink` | `[otel]` | OpenTelemetry GenAI spans/metrics/events (per-call `chat` spans, `invoke_agent` root, `execute_tool`, TTFT). Console exporter fallback without env. |
| `AgentOSHttpSink` | `[agentos]` | `POST /agentos/api/ingest/events` (batches `{events:[…]}`), presenting the `cak_` key as Bearer. Idempotent per `event_id`. |
| content sink | — | prompt/completion/tool capture (see capture env). |

**Event kinds:** `session_started`, `turn_started`, `user_message`, `assistant_message`, `tool_use`, `tool_result`, `usage_snapshot`, `response_first_token` (TTFT, OTel-only), `policy_decision`, `system_message`, `session_ended`.

### 9.2 Policy & guardrails

Tool-use authorization gates every tool call, fail-closed. Engines: **OPA** (remote, core), **Cedar** (in-process, `[cedar]`), **SRS** (AgentOS-managed RAI policies, auto-wired from `SRS_*` env). A `PolicyToolAuthorizer` adapts a `PolicyEngine` into a `PolicyDecider`. Because `can_use_tool` is skipped under `bypassPermissions`, enforcement rides a **`PreToolUse` hook** that maps a deny → `permissionDecision:"deny"` — so console-bound guardrails hold even in unattended runs.

### 9.3 AgentOS control plane (one config, server-owned paths)

The SDK needs only **`AGENTOS_DISCOVERY_URL`** + the **`cak_` key** (`COMPUTERAGENT_HARNESS_TOKEN`). `computeragent.agentos` GETs the discovery document once and resolves `ingest`/`api`/`otel` endpoints from it (absolute URLs, never constructs a path). Per-endpoint override envs win over discovery.

- **Ingest** — `AgentOSHttpSink` ships telemetry; the server projects it into the AgentOS collections so library-mode agents appear in the UI.
- **OTLP gateway** — `AGENTOS_OTEL_URL` → `OtelSink` injects the `cak_` Bearer to a server-side forwarder holding vendor creds.
- **Git credentials** — private GAP clones call `POST …/git-credentials/resolve` with the `cak_`; the server mints a short-lived GitHub-App installation token (`{host, username:"x-access-token", token}`).
- **Run-by-reference** — `agent_id` + no `source` → `POST …/agents/resolve` returns source/harness/model (group-scoped).
- **Remote harness** — `harness_url=` runs engine/substrate/loader on a remote server; the SDK client emits nothing locally by default (the server owns persistence + telemetry).

---

## 10. Process-level agent forking (`feat/process-fork`)

> Opt-in and **off by default**. Lets a running agent spawn **child `ComputerAgent` processes** — a separate Claude CLI, its own workspace, its own `ca_…` session — instead of (or alongside) the in-process Task sub-agent. Because a fork is a real `ComputerAgent`, each child gets its **own `agent_id`/`agent_name`/`session_id`**, linked to the parent. Code: `computeragent.harness.fork`.

### 10.1 Why
The native Claude Task/`AgentDefinition` sub-agent runs *inside* the parent's process and *shares* its session, so sub-agents can't be observed, guardrailed, or traced as first-class agents. Forking makes each sub-agent a genuine, independently-observable process.

### 10.2 Modes (all use the same `mcp__fork__spawn` tool)

| `fork_mode` | Behavior |
|---|---|
| `"off"` (default) | Nothing changes. Native in-process sub-agents work exactly as before. |
| `"manual"` | Exposes the `mcp__fork__spawn` tool; the agent forks when it chooses. Native sub-agents **also** remain available (both paths coexist). |
| `"auto"` | Disables native sub-agents (`agents={}`, `Task` disallowed) and steers **all** delegation to `mcp__fork__spawn` via a system-prompt line. Every delegation becomes a separate process. |

The mechanism is the MCP tool, not a hook: a `PreToolUse` hook can only *deny*, never return a tool result, so it can't run an external process and hand the answer back. (An optional hook can act as a stray-`Task` guard in auto mode.)

### 10.3 The tool — `mcp__fork__spawn`

```
spawn(task: str,                 # required — the subtask
      agent?: str,               # a preset from the parent's agents/<name>
      name?: str, role?: str,    # OR a dynamic child defined at call time
      model?: str, tools?: str[])
→ { content: [{ type:"text", text: "[fork child agent_id=… session=… cost=$…]\n<result>" }] }
```

- **Dynamic child** → `identity_loader="passthrough"` with a synthesized `system_prompt` (role), model, and tool allowlist.
- **Preset child** → the `AgentDefinition` from `agents/<name>` is materialized into the same passthrough child.
- **Workspace** → a fresh isolated `mkdtemp` per child (rmtree'd on dispose); opt-in `inherit_workspace` shares the parent's cwd.
- The async handler builds the child, `await`s its full run under a timeout, returns its result, and always disposes it (temp dir + subprocess).

### 10.4 `ForkConfig`

```python
from computeragent import ComputerAgent, ForkConfig

async with ComputerAgent(source="./gap-agent", identity_loader="gap",
                         fork_mode="auto",
                         fork_config=ForkConfig(max_depth=2, max_children=8,
                                                max_concurrency=4, child_timeout_s=300)) as agent:
    await agent.chat("Research X, Y, and Z — delegate each to a specialist.")
```

| Field | Default | Meaning |
|---|---|---|
| `max_depth` | `2` | A child may itself fork only while under this depth. |
| `max_children` | `8` | Lifetime fan-out cap per parent. |
| `max_concurrency` | `4` | Semaphore over concurrent children. |
| `child_timeout_s` | `300` | Per-child `asyncio.wait_for` timeout. |
| `inherit_workspace` | `False` | Share the parent's cwd instead of an isolated temp dir. |
| `inherit_options` | `True` | Inherit the parent's model + permission mode. |
| `allowed_presets` | `None` | Restrict which `agents/<name>` presets are forkable (`None` = any). |

**Safety:** these caps are net-new (the SDK has none). No external path/git-URL source is forkable (that would be clone-and-run remote code). Children are torn down on parent `dispose`/cancel (the SDK's `atexit` net kills orphaned processes but not their temp dirs).

### 10.5 Telemetry linkage
A child's coordinator stamps `parent_session_id` + `parent_agent_id` (added to `TelemetryEvent` and the ingest DTO) on every event, and derives `agent_id = "<parent>/<child>#<hex>"`. The server can nest child sessions under the parent (server-side rendering is a follow-up; the linkage is on the wire today).

### 10.6 Native sub-agent vs fork — at a glance

| | Native (Task/`AgentDefinition`) | Fork (`mcp__fork__spawn`) |
|---|---|---|
| Process | in the parent's `claude` process | a **separate** `claude` process |
| Session | shares the parent's | its **own** `ca_…` session |
| `agent_id` | none (shares parent) | derived `<parent>/<child>#…` |
| Observed/guardrailed independently | no | **yes** |
| Enabled when | always (unless `fork_mode="auto"`) | `fork_mode` ∈ {manual, auto} |

---

## 11. Environment variable reference

| Variable | Used by | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | claude engine, gitagent | Anthropic auth / gateway |
| `GITCLAW_MODEL_BASE_URL` + `OPENAI_API_KEY` | gitagent | OpenAI-compatible endpoint |
| `AGENTOS_DISCOVERY_URL` | agentos | one-config discovery of ingest/api/otel |
| `COMPUTERAGENT_HARNESS_TOKEN` | agentos, remote | the `cak_` key (Bearer everywhere) |
| `AGENTOS_INGEST_URL` / `AGENTOS_INGEST_TOKEN` | ingest sink | full-URL override / legacy static ingest token |
| `COMPUTERAGENT_HARNESS_URL` | remote | remote harness/CAS base URL |
| `COMPUTERAGENT_AGENT_ID` | harness | default stable agent id |
| `COMPUTERAGENT_OTEL` | telemetry | set `disabled` to turn off the OTel sink |
| `COMPUTERAGENT_OTEL_TEMPORALITY` | OtelSink | cumulative vs delta metric temporality |
| `COMPUTERAGENT_CAPTURE_CONTENT` / `_MODE` | content | capture prompt/completion/tool content (`attribute`/`event`/`both`) |
| `COMPUTERAGENT_MAX_ATTRIBUTE_LENGTH` | guardrails | attribute truncation length |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `_HEADERS` / `OTEL_SERVICE_NAME` | OtelSink | OTLP export target |
| `SRS_BASE_URL` / `SRS_API_KEY` / `SRS_POLICY_ID` / `SRS_PRINCIPAL_ID` | SRS policy | auto-wire SRS guardrails |
| `API_AUTH_USER` / `API_AUTH_PASS` | remote | Basic header for loopback harness calls |
| `COMPUTERAGENT_FORK_DEPTH` | fork | internal depth marker propagated to children |

---

## 12. Testing

- **Unit** (`tests/`, pytest): colocated, deterministic. `tests/test_fork.py` covers the fork option-injection, caps, linkage stamping, and the off-mode no-op — with fakes, no real Claude process.
- **Run:** `uv run pytest -q -m "not integration"`.
- **Live E2E harnesses** (not part of the unit suite; require `ANTHROPIC_API_KEY`): exercise real forked processes — capturing actual Claude CLI PIDs from the SDK's `_ACTIVE_CHILDREN` registry, live tool execution by children, cap enforcement, cleanup (no leaked temp dirs / orphan processes), a three-way `off`/`manual`/`auto` comparison, and a negative control proving off-mode creates zero child processes.

---

## 13. Versioning & publishing

`uv build` → `dist/*.whl` + sdist. Published to PyPI via Trusted Publishing (OIDC) on `v*.*.*` tags; manual fallback `uv publish --username __token__ --password $PYPI_TOKEN`. See [`CHANGELOG.md`](../CHANGELOG.md).

---

## 14. Where things live

| Concern | Module |
|---|---|
| Harness client / lifecycle | `harness/__init__.py`, `harness/coordinator.py` |
| Process forking | `harness/fork.py` |
| Remote harness / CAS | `harness/remote.py`, `harness/cas_client.py`, `harness/protocol.py` |
| Run-by-reference / git creds | `harness/agent_resolve_client.py`, `harness/git_credential_client.py` |
| Engines | `engines/claude_agent.py`, `engines/git_agent.py` |
| Substrates | `substrates/local.py` |
| Identity / GAP | `identity/{passthrough,gap,gap_subagents,gap_tools,gap_hooks}.py` |
| Session stores | `session_stores/memory.py` |
| Telemetry | `telemetry/{config,event,pipeline}.py`, `telemetry/middleware/*`, `telemetry/sinks/*` |
| Policy | `policy/{authorizer,opa,cedar,srs,types}.py` |
| AgentOS discovery | `agentos.py` |
| Drop-in proxy | `_proxy/{query,client}.py` |
| Protocols / types | `protocol_types/__init__.py`, `types.py` |

---

*This document reflects `computer-agent-py` 0.2.3 plus the `feat/process-fork` branch. Keep it in sync when adding an axis implementation, a sink, an env var, or a fork capability.*
