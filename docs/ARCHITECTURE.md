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

## Orchestration

`orchestration/` is the sub-agent fan-out, ported from the TypeScript harness-server's services. The
unit a worker runs on is a second `HarnessCoordinator`, parent-linked — this SDK has no session
registry because one `ComputerAgent` is one session, so `OrchestratorRef` is the snapshot a fan-out
reads off its parent instead.

The layering is deliberate: `contract.py` is pure types, then a tier of pure functions
(`parse_roster`, `crew`, `worker_mcp`, `budget`, `scorecard`, `globmatch`, `worker_digest`) that
touch nothing, then `worktree.py` (real git) and `spawn_child.py` (real sessions), then the two
dispatchers and the coordinator drive above them. Everything below `spawn_child` is testable without
an engine, which is where most of the behaviour that used to break in production lives.

Each divergence from the TypeScript original is documented at the site that makes it, and listed in
the changelog under the release that introduced it.

## Deferred

Nothing from the original plan set remains deferred. Design notes written against the TypeScript
implementations are in [`plans/`](plans/).
