# Architecture

For anyone changing the SDK. If you only want to *use* it, read
[`USING_THE_SDK.md`](USING_THE_SDK.md) instead.

## The shape of a run

```
ComputerAgent(…)            every setting: argument, else its environment variable
        │
        │  constructor, synchronous — fails before anything runs
        ├─▶ settings.resolve_settings()        → Settings (missing ones reported together)
        ├─▶ agentos_agent.resolve_agent()      GET {base_url}/v1/agents/{id}   x-api-key
        │       └─ ResolvedAgent: harness, model, mcp_mode, mcp_bindings
        ├─▶ gateway_envs.build_gateway_envs()  → the engine's environment (llm_url)
        ├─▶ gateway_envs.build_options()       → permission mode, model, caller options
        ├─▶ mcp.build_mcp_servers()            → engine MCP servers on mcp_url
        ├─▶ SrsToolGate + PolicyToolAuthorizer → the tool-call gate
        ├─▶ telemetry.build_pipeline()         → the OTLP sink on telemetry_url
        │
        └─▶ HarnessCoordinator
                │  lazily, on the first chat()
                ├─▶ LocalSubstrate.boot()        → workspace (gap_dir, or the cwd)
                ├─▶ identity loader              → AgentManifest (gap | passthrough)
                │  every chat()
                ├─▶ SrsTurnGuard.start_turn()    → input check (guardrails_url /v1/check)
                └─▶ EngineDriver.start_session() → EngineEvent stream
                        │   every tool call → SrsToolGate (tool-input check, by tool-use id)
                        └─▶ TelemetryEvent → middleware → sinks
```

`ChatHandle` drives the coordinator through a two-method protocol (`run_chat`, `dispose`), so it
has no knowledge of what is underneath it.

## Every call goes through an egress route

Each downstream service is reached only at a configured URL: `base_url`, `guardrails_url`,
`llm_url`, `mcp_url`, and optionally `telemetry_url`. There is no
fallback to a discovered or default destination, and the engine env switches off the Claude CLI's
own background traffic. Every request carries `x-api-key: <api_key>`, which the egress proxy swaps.
`Settings.api_key()` reads the environment at each use, so the key can be replaced while an agent
runs.

## Why resolution happens in the constructor

Everything the run needs is decided before anything runs. A missing setting, a bad id, an agent
shape this SDK does not implement — all of it raises from `ComputerAgent(...)`, where the error is
cheap and attributable, rather than inside a subprocess. This is why `resolve_agent` is synchronous
despite being an HTTP call.

## The modules that carry the run mode

| Module | Responsibility |
|---|---|
| `harness/settings.py` | Resolve every setting from its argument or environment variable. |
| `harness/agentos_agent.py` | Fetch and validate the agent document; refuse unimplemented shapes by field name. |
| `harness/gateway_envs.py` | The engine environment for `llm_url`, and the model string per the TS dispatch. |
| `harness/mcp.py` | AgentOS MCP bindings → engine MCP servers on `mcp_url` (port of CAS `applyMcpServers`). |
| `harness/guardrails.py` | The per-turn input check and its telemetry. |
| `harness/telemetry.py` | The pipeline built from `telemetry_url`. |
| `harness/__init__.py` | `ComputerAgent`, `ChatHandle`, `ChatResult`, and the wiring above. |
| `harness/coordinator.py` | Boot, turn lifecycle, the turn guard hook, engine-event → telemetry translation. |
| `policy/guardrails_client.py`, `policy/srs_gate.py`, `policy/srs_input.py` | The `/v1/check` client, the tool gate and the input check (folding ported from CAS). |
| `engines/claude_agent.py` | Wraps `claude_agent_sdk.query()`; builds `ClaudeAgentOptions`. |

## Decisions worth knowing before you change something

**The gateway owns policies and fail-open.** The SDK sends `input` and `tool-input` checks to
`{guardrails_url}/v1/check` with no policy ids; the agent gateway applies the org's policies and then
the agent's own. A check with no verdict blocks: when the gateway fails open it answers `allow` with
`skipped: true`. A tool call is identified by the engine's own tool-use id, which
`PolicyToolAuthorizer` puts in the policy input as `call_id`; a call without one is denied.

**A blocked input never starts the engine.** The coordinator streams a single `ResultMessage`
carrying the reply instead, through the normal event path, so `ChatResult` and telemetry look like
any other turn.

**GAP mode is explicit.** `gap_dir` selects the GAP loader; nothing is inferred from whether an
`agent.yaml` happens to exist. `agent.yaml` MCP servers are refused because they name their own URLs.

**MCP servers are merged into the loader's, not overlaid.** The coordinator adds the bound servers
to whatever the identity loader resolved before the caller's options overlay, so a GAP agent's
in-process `gap_tools` server is kept.

**The session store is the upstream contract, re-exported.** `protocol_types` re-exports
`SessionStore` / `SessionKey` / `SessionStoreEntry` from `claude_agent_sdk.types`. A store goes
straight to the engine, which mirrors transcript batches into it; the coordinator does **not** also
replay it, which would double the transcript.

**Model-route envs layer over caller envs, not under.** `llm_url` owns LLM routing, so a caller
cannot point the engine elsewhere. `CLAUDE_CODE_USE_BEDROCK` is dropped because the CLI honours it
and ignores `ANTHROPIC_BASE_URL` entirely.

**Policy rides a `PreToolUse` hook under prompt-skipping permission modes.** `can_use_tool` is
skipped entirely under `bypassPermissions` / `dontAsk` / `auto` / `acceptEdits` — which is what
AgentOS dispatches — so the gate is also wired as a hook that merges with existing hooks. A decider
that raises fails closed.

## The four axes, and which still move

| Axis | State |
|---|---|
| Engine | **Swappable** — `claude-agent-sdk`, `gitagent`, or a caller `EngineDriver`. Registry in `engines/`. |
| Session store | **Swappable** — `memory`, `mongo`, `http`, or any duck-typed store. |
| Substrate | Fixed: `LocalSubstrate`, a directory that already exists. |
| Identity loader | Fixed per run: GAP when `gap_dir` is set, passthrough otherwise. |

## Telemetry

A `ComputerAgent` builds its own pipeline from `telemetry_url` (OTLP); unset, telemetry is off and the module-level, environment-driven pipeline is not used. The
coordinator owns the hot path — engines never call `pipeline.emit`, or events would double-fire
when an engine is composed under the harness. One `ComputerAgent` is one logical session:
`session_started` on the first chat, `session_ended` on dispose, so N chats are N turns of one
conversation rather than N orphan sessions. Every event is stamped with `agent_id` at a single
chokepoint.

The drop-in proxy (`_proxy/`) keeps its own emit for standalone `query()` use, which is why it is a
separate surface rather than a mode of `ComputerAgent`.

## Deferred

Sub-agent orchestration is refused at resolve time rather than silently degraded. Design notes,
written against the TypeScript implementation, are in [`plans/`](plans/).
