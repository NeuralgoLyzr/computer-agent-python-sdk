# Architecture

For anyone changing the SDK. If you only want to *use* it, read
[`USING_THE_SDK.md`](USING_THE_SDK.md) instead.

## The shape of a run

```
ComputerAgent(base_url, agent_id, api_key, engine, …)
        │
        │  constructor, synchronous — fails before anything boots
        ├─▶ agentos_agent.resolve_agent()   GET {base}/v1/agents/{id}   x-api-key
        │       └─ ResolvedAgent: source, harness, model, gateway, credential_id
        ├─▶ gateway_envs.build_gateway_envs()   → the engine's environment
        ├─▶ gateway_envs.build_options()        → permission mode, model, caller options
        │
        └─▶ HarnessCoordinator
                │  lazily, on the first chat()
                ├─▶ LocalSubstrate.boot()       → workspace (a clone, or a caller Path)
                ├─▶ _AutoIdentityLoader.load()  → AgentManifest (gap | passthrough)
                └─▶ EngineDriver.start_session() → EngineEvent stream
                        │
                        └─▶ TelemetryEvent → middleware → sinks
```

`ChatHandle` drives the coordinator through a two-method protocol (`run_chat`, `dispose`), so it
has no knowledge of what is underneath it.

## Why resolution happens in the constructor

Everything the run needs is decided before anything is booted, cloned or spawned. A bad id, a
missing proxy, an agent shape this SDK does not implement — all of it raises from
`ComputerAgent(...)`, where the error is cheap and attributable, rather than three seconds into a
subprocess. This is why `resolve_agent` is synchronous despite being an HTTP call.

## The five modules that carry the run mode

| Module | Responsibility |
|---|---|
| `harness/agentos_agent.py` | Fetch and validate the agent document; refuse unimplemented shapes by field name. |
| `harness/gateway_envs.py` | Port of the server's `modelGatewayEnvsFor` — the three model-routing branches. |
| `harness/__init__.py` | `ComputerAgent`, `ChatHandle`, `ChatResult`, and the identity-loader choice. |
| `harness/coordinator.py` | Boot, turn lifecycle, engine-event → telemetry translation. |
| `engines/claude_agent.py` | Wraps `claude_agent_sdk.query()`; builds `ClaudeAgentOptions`. |

## Decisions worth knowing before you change something

**The identity loader is chosen after the source materialises.** GAP when `agent.yaml` is at the
workspace root, passthrough when it is not. `GapIdentityLoader` raises rather than degrading, so
deciding earlier would make a plain (non-GAP) repo unrunnable.

**The git token is a credential mapping, not a URL rewrite.** The TS server splices the token into
the clone URL because its envs are dropped by some substrates. Ours never are, so the substrate
injects an `http.<host>.extraHeader` instead and the token reaches neither `argv` nor
`.git/config`.

**The session store is the upstream contract, re-exported.** `protocol_types` re-exports
`SessionStore` / `SessionKey` / `SessionStoreEntry` from `claude_agent_sdk.types` rather than
defining lookalikes. That is what `ClaudeAgentOptions.session_store` accepts, so a store goes
straight to the engine — the SDK mirrors transcript batches into it and materialises prior entries
before spawning the subprocess. The coordinator deliberately does **not** also replay it; doing
both would double the transcript. The protocol is not runtime-checkable upstream, so stores are
duck-typed on `append`/`load`.

**Gateway envs layer over caller envs, not under.** A resolved gateway owns LLM routing, so a
caller cannot accidentally point a gateway-bound agent elsewhere. `CLAUDE_CODE_USE_BEDROCK` is
additionally dropped when a gateway resolved, because the CLI honours it and ignores
`ANTHROPIC_BASE_URL` entirely.

**Policy rides a `PreToolUse` hook under prompt-skipping permission modes.** `can_use_tool` is
skipped entirely under `bypassPermissions` / `dontAsk` / `auto` / `acceptEdits` — which is exactly
what AgentOS dispatches — so a policy bound only there would go unenforced. The hook merges with
existing hooks rather than replacing them, and a decider that raises fails closed.

## The four axes, and which still move

The harness was built around four swappable axes. Two are now fixed by the run mode:

| Axis | State |
|---|---|
| Engine | **Swappable** — `claude-agent-sdk`, `gitagent`, or a caller `EngineDriver`. Registry in `engines/`. |
| Session store | **Swappable** — `memory`, `mongo`, `http`, or any duck-typed store. Registry holds *builders*, since mongo and http are configured per call. |
| Substrate | Fixed: `LocalSubstrate`. The run mode is "run it here". |
| Identity loader | Fixed: GAP with a passthrough fallback, chosen automatically. |

## Telemetry

The coordinator owns the hot path — engines never call `pipeline.emit`, or events would double-fire
when an engine is composed under the harness. One `ComputerAgent` is one logical session:
`session_started` on the first chat, `session_ended` on dispose, so N chats are N turns of one
conversation rather than N orphan sessions. Every event is stamped with `agent_id` at a single
chokepoint.

The drop-in proxy (`_proxy/`) keeps its own emit for standalone `query()` use, which is why it is a
separate surface rather than a mode of `ComputerAgent`.

## Deferred

MCP support and sub-agent orchestration are refused at resolve time rather than silently degraded.
Design notes for both, written against the TypeScript implementations, are in [`plans/`](plans/).
