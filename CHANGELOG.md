# Changelog

All notable changes to `computer-agent-py` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] — unreleased

Every call the SDK makes now matches a route the CAS egress proxy serves (plan
`.claude/plans/p1-sdk-egress-routes.md`).

### Added

- **Sandbox token reissue is adopted by every client.** An egress answer that
  carries `X-Cas-Token` / `X-Cas-Token-Expires-At` (an `sbt_` token with a later
  expiry, on a request sent with the newest token) makes every later egress
  request carry the new token: the agent-document read, the guardrails checks,
  gitagent's Anthropic/OpenAI clients and injected httpx clients.
  `Settings.api_key()` returns the newest token and `LYZR_API_KEY` is updated
  (`computeragent.harness.egress_token`).
- **A loopback forwarder for the Claude CLI.** On the `claude-agent-sdk` engine
  the CLI's `ANTHROPIC_BASE_URL` and MCP server URLs on an egress origin point at
  a listener on `127.0.0.1` that replays each request on the proxy with the
  newest token and streams the answer back
  (`computeragent.harness.egress_forwarder`). `h11` joins the `agentos` extra.
- **OTLP exports fit the egress proxy's 1 MiB limit.** Each export is split on
  its encoded size (960 KiB budget, `computeragent.telemetry.sinks.otlp_size`);
  a single record over the budget is dropped with a WARN carrying the count.
  Span and log processors export 128 records at a time. The OTLP exporters set
  `x-api-key` on every request through a stamping `requests` session, so a
  reissued sandbox token applies to telemetry too.
- **`COMPUTERAGENT_SESSION_STORE_URL` / `session_store_url`.** The base URL of
  the `http` session store behind the egress proxy
  (`http://egress/computer-agent-api`); it overrides a descriptor's `base_url`
  for kind `http` and is ignored for other kinds and store instances
  (`computeragent.session_stores.with_base_url`).

### Changed

- **BREAKING: guardrails go through the egress proxy.** The user's message and
  every tool call are checked with `POST {guardrails_url}/v1/check` (ops
  `input` and `tool-input`); the agent gateway applies the org's and the
  agent's RAI policies and owns fail-open. `guardrails_url` /
  `COMPUTERAGENT_GUARDRAILS_URL` is required and replaces `srs_url` /
  `COMPUTERAGENT_SRS_URL` and `agent_api_url` / `COMPUTERAGENT_AGENT_API_URL`,
  which are no longer read.
- A check that reaches no verdict (unreachable, 5 s timeout, non-2xx, a
  malformed answer) blocks the message or denies the tool; the SDK applies no
  fail-open of its own.
- A tool call is identified by the engine's own tool-use id: the Claude
  engine's `PreToolUse` hook passes it, `PolicyToolAuthorizer` puts it in the
  policy input as `call_id`, and a call without one is denied.
- A rewritten message carries the gateway's masked text (`[EMAIL]`).
- **BREAKING: MCP servers are addressed by name.** Each bound server is
  `{mcp_url}/{tool_name}` (one percent-encoded segment) with no query; the
  server id path and the `?tools=` query are gone, since the gateway resolves
  both from its registry. Behind the egress proxy `COMPUTERAGENT_MCP_URL` is
  `http://egress/mcp/v1`.
- **BREAKING: the `http` session store sends only `x-api-key`.** The store
  token (`x-ca-session-store-token`), its refresh against `/v1/auth/token`, the
  `token` / `auto_refresh` arguments, `TOKEN_HEADER` and `expiry_of` are gone;
  a `token` option is ignored. The key is `LYZR_API_KEY` at each request, as
  the newest sandbox token.
- A session-store descriptor without a `kind` is reported by its keys, never
  its values.

### Removed

- **BREAKING: the SDK's own policy resolution.** No org RAI policies are read
  (`/v3/org/rai-policy`), the agent document's `rai_policy_id` is ignored and
  `ResolvedAgent.rai_policy_id` is gone, and no SRS route
  (`/v1/rai/inference`, `/v1/guardrails/evaluate-tool-call`) is called.
  `computeragent.policy.srs_client`, `computeragent.policy.org_policy`,
  `TurnPolicy`, `build_turn_policy`, `SrsToolGate.set_turn` and
  `SrsTurnGuard.resolve_turn` are deleted. The SDK no longer runs a separate
  content check on a tool call's string arguments; the tool call goes to the
  gateway as one `tool-input` check.
- **BREAKING: the standalone SRS policy engine.** `SrsPolicyEngine`,
  `srs_authorizer_from_env` and `computeragent.policy.srs` are gone; nothing
  fetches `{srs}/rai/policies/{id}` any more. Use the built-in guardrails gate,
  or `OpaPolicyEngine` / `CedarPolicyEngine` for a gate of your own.
- **BREAKING: the AgentOS dashboard events sink.** `AgentOSHttpSink`
  (`computeragent.telemetry.sinks.agentos_http`), the `events_url` setting and
  `COMPUTERAGENT_EVENTS_URL` are gone, and the drop-in path no longer
  auto-attaches the sink from `AGENTOS_INGEST_URL` / discovery. The egress
  proxy serves no `/events` route. `examples/with_agentos.py` is deleted;
  `examples/multi_sink.py` and `examples/e2e/run_live_demo.py` keep their
  `OtelSink`.

## [0.6.0] — 2026-10-23

Every call the SDK and its engine make now goes through an egress proxy, the agent is baked into
the image, and the SRS guardrails CAS's harness applies run in the SDK.

### Added

- **Every setting from its argument or its environment variable.** `agent_id`, `engine`,
  `base_url`, `api_key` and the routes below each fall back to a variable (`COMPUTERAGENT_*`,
  `LYZR_API_KEY` for the key); the argument wins and a blank value counts as unset. With the
  variables set, `ComputerAgent()` needs no arguments. Every missing required setting is reported in
  one `SettingsError` (exported from `computeragent`) naming both spellings, before any request.
  `api_key` from the environment is read at each use, so it can be replaced while an agent runs.
- **Egress routes**, each reached only at its configured URL with the key on `x-api-key`:
  `llm_url` (model calls), `mcp_url` (MCP), `agent_api_url` (org RAI policies), `srs_url`
  (guardrails), and optionally `telemetry_url` (OTLP) and `events_url` (AgentOS dashboard events,
  `POST {events_url}/events`).
- **MCP servers bound in AgentOS** now run, through `mcp_url`, instead of being refused. Bindings come
  from the agent document (`mcp_servers`, `mcp_mode`); each becomes `{mcp_url}/{server_id}?tools=…`
  named by its `tool_name`. A "require user sign-in" binding uses the caller's own server
  (`your_server_id`), and is withheld with a warning when the caller has none. `mcp_mode` other than
  `"engine"` attaches nothing. A port of CAS `mcpDescriptorsFor` / `applyMcpServers`.
- **SRS guardrails, ported from CAS's harness.** At the start of every turn the org (global) RAI
  policies are read from `{agent_api_url}/v3/org/rai-policy` and combined with the agent's own
  (local) `rai_policy_id`, org first, de-duplicated. Against that one set:
  - the user's message is checked with SRS `llm_input` before the turn — a block ends the turn with
    the reason as its reply and the engine never starts; a mask is what the engine receives;
  - every tool call is checked with SRS's content check and then Cedar/OPA (`evaluate-tool-call`),
    on both engines, with tool names presented as `<engine>__<tool>` / `mcp__<server>__<tool>`.

  No verdict denies, unless the org policies' `fail_open` allows it (a 422 never fails open); org
  policies that cannot be read block the turn. Decisions are emitted as `policy_decision` events,
  never with content. A caller's own `policy_decider` replaces the tool gate; the input check always
  runs. A streamed (async-iterable) message is checked entry by entry before the engine starts.
- **GAP mode** through `gap_dir` (`COMPUTERAGENT_GAP_DIR`): the agent is loaded from that directory's
  `agent.yaml`. The directory must exist and hold an `agent.yaml`.
- `ResolvedAgent.rai_policy_id`, `.mcp_mode` and `.mcp_bindings`.

### Changed

- **BREAKING: `llm_url`, `mcp_url`, `agent_api_url` and `srs_url` are required.** Model calls go to
  `llm_url` verbatim, for every engine and agent; the agent document's gateway URL is not used and
  there is no direct-to-Anthropic branch. The engine env also sets
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and `DISABLE_AUTOUPDATER=1`, so the Claude CLI's own
  background traffic does not go around the proxy.
- **BREAKING: the agent is baked into the image.** The engine runs in the working directory
  (`options={"cwd": ...}` still wins) with the agent defined by your `options`, or in GAP mode from
  `gap_dir`. An `agent.yaml` in the working directory no longer switches the loader on its own.
- **BREAKING: a `ComputerAgent` builds its telemetry pipeline from `telemetry_url` and
  `events_url`.** `AGENTOS_DISCOVERY_URL`, `AGENTOS_INGEST_URL`, `AGENTOS_OTEL_URL` and
  `OTEL_EXPORTER_OTLP_ENDPOINT` are no longer consulted by `ComputerAgent`; with neither route set
  it sends no telemetry. A caller's own `telemetry_pipeline` still wins. The drop-in `query()` path
  and `configure()` are unchanged.
- **BREAKING: GAP `agent.yaml` `mcp_servers` are refused**, because they connect to their own URLs.
  Bind those servers to the agent in AgentOS.
- `AgentOSHttpSink` takes an optional `api_key` callable, read at every POST and sent as
  `x-api-key`.

### Removed

- **BREAKING: git cloning**, `ComputerAgent(source=, git_token=, llm_proxy_url=)`, the
  `/up/<b64url>` and `/v4` URL building (`b64url`, `UPSTREAM_PATH`), the substrate's
  `git_credential` parameter, `SubstrateHandle.revision`, and `ResolvedAgent.source` /
  `.credential_id`.

### Migration

Set the routes your egress proxy exposes and drop the removed arguments:

```python
ComputerAgent(
    agent_id=..., engine="claude-agent-sdk", api_key=...,
    base_url="http://egress/agentos/api",
    agent_api_url="http://egress/agent-api",
    srs_url="http://egress/srs",
    llm_url="http://egress/llm",
    mcp_url="http://egress/mcp",
)
```

or set `COMPUTERAGENT_*` / `LYZR_API_KEY` and call `ComputerAgent()`. A GAP agent passes
`gap_dir=` instead of `source=`.

## [0.5.0] — 2026-09-24

### Changed

- **BREAKING: `engine` is now a required argument to `ComputerAgent`.** It used to default to the
  agent document's `harness`. That default was more load-bearing than it looked: `engine` selects
  the driver *and* which gateway-env branch `build_gateway_envs` produces, so a `harness` edit in
  AgentOS could silently re-shape `ANTHROPIC_BASE_URL` and the model string under a caller who
  never asked for either. Naming it makes the run reproducible from the call site.

  Migration: pass `engine="claude-agent-sdk"` (the previous effective default for every agent whose
  document did not say otherwise) or `engine="gitagent"`, or an `EngineDriver` instance.

  `require_runnable_harness` is no longer called during construction — with the engine named by the
  caller, `resolve_engine` is the gate, and it raises `UnknownEngineError` listing what *is*
  registered. The function remains exported for callers who validate a document on their own. An
  engine that differs from the agent's declared `harness` is still legal (that is how you run an
  unsupported harness) and now logs a warning naming both, because it also moves the env branch.

- **Telemetry no longer prints to the console by default.** With the `[otel]` extra installed and
  no `OTEL_EXPORTER_OTLP_ENDPOINT` configured, `OtelSink` fell back to the OTel *console*
  exporters — so every span, every 5-second metric cycle and every log record was pretty-printed
  as JSON on stderr, burying the program's own output in a trace nobody asked for. It also left a
  `PeriodicExportingMetricReader` ticking on a daemon thread, which produced
  `Exception while exporting metrics: I/O operation on closed file` at interpreter shutdown.

  No endpoint now means `exporter="none"`: providers are still built and every instrumentation
  path runs unchanged, but nothing is exported and no metric reader is started. Console is opt-in
  via `configure(exporter="console")`. Callers passing their own provider, or an OTLP endpoint,
  see no change.

### Documentation

- **What actually restricts an agent's tools.** `allowed_tools` is a permission *pre-approval*
  list and blocks nothing in any mode; the previous wording implied `permission_mode="default"`
  would turn it into a whitelist. Verified against a live agent: under `default` the model invoked
  a tool outside the list and the decider was never consulted. `policy_decider` under the default
  `bypassPermissions` is the gate that holds — the engine routes it through a `PreToolUse` hook
  that fires on every tool call. Documented in §5 with the case each lever is for.

- Corrected two docstrings that still described the `runtime=` argument removed in 0.4.0
  (`harness/errors.py`, `substrates/__init__.py`).

## [0.4.3] — 2026-09-16

### Removed

- **The GitHub-App credential fallback.** The chain was `git_token=` →
  AgentOS's `/git-credentials/resolve` → anonymous clone; the middle step is
  gone. That route is App-only (`githubAppConfigured()` then
  `mintInstallationToken()`), while these deployments authenticate with PATs —
  held on the agent as `gitToken`, or in the Lyzr credential vault behind its
  `credential_id`. The App route can return neither, so the fallback could not
  succeed; it only turned a missing token into a slower, less legible failure,
  since `resolve_git_credential` swallowed every outcome and a 403, a 404 and a
  503 all surfaced as an anonymous clone failing on git auth.
- `harness/git_credential_client.py`, the two coordinator parameters that
  existed only to reach that route, and `test_git_credentials_integration.py` —
  which had been failing continuously while exercising infrastructure these
  deployments do not use. Integration is now green.

The substrate's credential injection is unchanged: an explicit token is still
handed over as a mapping and injected as an `http.<host>.extraHeader`, so it
reaches neither argv nor `.git/config`.

## [0.4.2] — 2026-09-16

### Changed

- **`claude-agent-sdk` pinned at `>=0.2.152`** (was `>=0.2`, resolving to
  0.2.87 — sixty-six releases behind). Includes a zombie-CLI-subprocess fix,
  argv-injection hardening, and MCP 2.x. The three behaviours that could have
  broken were each checked live: GAP skill mirroring, the in-process
  `gap_tools` MCP server, and the widened message union.
- **`project_key` removed.** It never reached the engine — the session-store key
  is the SDK's own, derived from the workspace cwd — so it was a knob wired to
  nothing.
- **`budget_max_usd` now means what its name says**, mapping to the
  `max_budget_usd` option the Claude CLI actually enforces. An explicit option
  still wins.
- **The `sqlite` extra is gone.** It promised a session store that does not
  exist.

### Fixed

- **Concurrent turns on one `ComputerAgent` are refused.** They used to
  interleave silently; since turns now resume the previous engine session, two
  at once would share one session, one workspace and one store key.
- **Options the engine cannot accept are logged**, naming them and the upstream
  version. Dropping them silently made a typo — or an option renamed upstream —
  fail invisibly.
- **A temp clone left by a process that skipped `dispose()` is reaped at exit.**
  Not a finalizer: `__del__` runs at an arbitrary point in async teardown.
- **An inline `dict` source carrying `files` is refused.** The files were not
  materialised and the workspace fell back to the current directory, so the
  agent ran against the caller's cwd instead of the files they listed.

### Testing

Five verification gaps closed. `gitagent` proven live against the gateway
(445 in / 8 out, where it previously raised). Cross-process resume proven
through the mongo store — one process writes, a separate interpreter recalls.
The `git://` clone branch now covered with a local `git daemon`, needing no
credential. The httpx-missing hint covered without venv surgery.

## [0.4.1] — 2026-09-16

A survey of ~180 usage combinations against the live stack found six things that
did not work and a set of inputs that failed late or plausibly-and-wrong. All
are fixed; each of the six was verified live.

### Fixed

- **Multi-turn conversation had no memory.** Every `chat()` started a fresh
  engine session, so a session store's key differed each turn and nothing was
  rehydrated. Turns after the first now `resume` the previous engine session.
  (`resume`, not `session_id` — the CLI refuses an id that already exists,
  which is why the previously documented workaround failed on turn two.)
- **`gitagent` could not run at all.** It read four variables from
  `os.environ` while the harness delivers them on `EngineContext.envs`, so
  every value the gateway branch computed was invisible to it.
- **Conversation usage was billed as its final turn.** `session_ended` was
  built from the last `ResultMessage`; the lifecycle rollup now sums finished
  turns.
- **`chat()` after `dispose()` ran against a deleted workspace**, and emitted
  no `session_ended`. It raises.
- **`engine=` could not rescue an unsupported harness**, though the error said
  to try it. That check moved out of document validation into construction.
- **`allowed_tools` restricted nothing** under the `bypassPermissions` this SDK
  dispatches; the engine now warns when both are set. The e2e example shipped
  such an allowlist and called "Bash ran anyway" a pass.
- **Inputs that used to fail late or silently** are refused at construction:
  an empty `source` (which resolved to the process's cwd and pointed a
  bypassPermissions agent at it), a nonexistent or non-directory source path, a
  wrong-typed source, a malformed `llm_proxy_url`, and a non-string `base_url`
  (a bare `AttributeError` before).
- **A `base_url` typo is refused rather than appended to.** `…/agentos/apiv1`
  became `…/agentos/apiv1/agentos/api/v1`, and the resulting 404 blamed the
  caller's agent id.
- **`engine=object()` / `session_store=object()`** said "unknown *name*"; they
  now say the value is not a driver or store.
- **`on_tool_call` is never consulted on the claude engine** — accepted in
  silence, so a caller believed they had a tool gate. It warns.
- **`cancel()` before the first iteration** was a no-op and the turn ran to
  completion. It now prevents the run.
- `__all__` listed `SessionStore` and `SessionStoreEntry` twice.

### Documentation

`docs/USING_THE_SDK.md` gains a public-API and import table, how to extract the
assistant's reply (`messages[-1]` is a `ResultMessage`; the text is `.result`),
the real `EngineEvent` shape, async/concurrency and timeout behaviour, and the
telemetry defaults. Corrected: the Claude CLI ships **bundled** with
`claude-agent-sdk` and needs no `npm install`.

## [0.4.0] — 2026-09-16

### Changed — BREAKING: one run mode

`ComputerAgent` took eleven ways to start an agent and reliably delivered none of
them against the live stack. It now takes one: name an agent registered in
AgentOS and run it here.

```python
ComputerAgent(base_url=..., agent_id=..., api_key=...)
```

The SDK fetches the agent's definition and reproduces what the AgentOS dispatch
path would have sent to a ComputerAgent server — same source, model, engine
environment and permission mode — except the engine runs in this process.
Everything beyond the three required arguments is an override for something the
agent document already answers.

### Removed

- The bare `/v1` harness-protocol client, the CAS sandbox client, the remote
  coordinator, process forking, and the old `POST /agents/resolve` client
  (~1,600 lines), with their tests.
- `run_task`, `ForkConfig`, `HarnessClient`, `ComputerAgentServerClient`,
  `HarnessProtocolError`, and the `remote` extra.
- Constructor parameters `runtime`, `identity_loader`, `harness*`, `fork_*`,
  `parent_*`, `policy_id`.
- Five examples built on the standalone `source=`/`engine=` constructor, and
  `user-documentation.md` — superseded by `docs/USING_THE_SDK.md`.

The drop-in `query` / `ClaudeSDKClient` surface is untouched. It is a different
surface, not a run mode.

### Added

- `ComputerAgent(base_url=, agent_id=, api_key=)` — resolve an agent from
  AgentOS by its Mongo `_id`, presenting the caller's Lyzr workspace key on
  `x-api-key`. Unimplemented agent shapes (a generic gateway, bound MCP servers,
  an orchestration block, an unknown harness) are refused **by field name**
  rather than run as something quieter than they declare.
- `llm_proxy_url=` and `git_token=`, each needed only in a specific case: the
  first for an Anthropic-shaped engine against a native (OpenAI-shaped) gateway,
  the second for a private source. Missing either where it is required is an
  error at construction, not a failure mid-run.
- **Session stores that work.** The axis was inert: a store could be configured
  and no engine ever read it. It now targets the upstream
  `claude_agent_sdk.SessionStore` contract, so a store reaches the engine and
  resume actually happens. `mongo` and `http` ported from the TypeScript stores;
  the registry now holds builders, so a kind can be configured per call.
- `examples/e2e/live_agent_run.py` — a staged live run that prints pass/fail per
  stage, including a preflight that separates a dead gateway from a stopped
  proxy from a bad key.
- `docs/USING_THE_SDK.md`, a developer reference, and design notes for the
  deferred MCP and orchestration work in `docs/plans/`.

### Fixed

- `policy.srs` bound `httpx` at import time while `policy/__init__` documented
  its engines as lazily imported. It is now lazy, as advertised.
- The `mongo` session store built without a `url` raised a bare `TypeError`; it
  now names the option to supply.

## [0.3.0] — 2026-08-21

### Added

- **Process-level agent forking** (opt-in; off by default). A running
  `ComputerAgent` can spawn **child `ComputerAgent` processes** — each its own
  Claude CLI subprocess, isolated workspace, and `ca_…` session — instead of
  (or alongside) the native in-process Task sub-agent, via a new
  `mcp__fork__spawn` MCP tool (`computeragent.harness.fork`). Children get their
  own `agent_id`/`agent_name`/`session_id`, linked to the parent through new
  `parent_session_id`/`parent_agent_id` fields on `TelemetryEvent` and the
  AgentOS ingest DTO.
  - `ComputerAgent(fork_mode="manual")` exposes the tool; `"auto"` disables
    native sub-agents and routes all delegation to forks; `"off"` (default) is a
    no-op.
  - Children may be **dynamic** (`name`+`role`+`model`+`tools` at call time) or a
    **preset** (`agent=<name>` materialized from the repo's `agents/<name>`).
  - `ForkConfig` safety caps: `max_depth`, `max_children`, `max_concurrency`,
    per-child `child_timeout_s`, plus guaranteed temp-dir + subprocess teardown.
  - New public export `ForkConfig`.
- **Documentation:** `docs/ARCHITECTURE.md` (end-to-end package reference) and
  `user-documentation.md` (user guide: console connection URLs, usage, forking).

## [0.2.3] — 2026-07-02

### Changed

- Examples, tests, and docstrings now use a generic placeholder agent name.
  Renamed `examples/pdf_drop_in.py` → `examples/drop_in.py` and
  `tests/test_pdf_snippet.py` → `tests/test_drop_in.py`; the drop-in narrative
  is now told against the upstream SDK rather than a specific deployment.

### Fixed

- `computeragent.__version__` was stuck at `0.2.1` (missed in the 0.2.2 bump);
  it now tracks the packaged version.

## [0.2.2] — 2026-06-26

### Added

- **`ComputerAgent(..., policy_id=...)`** — attach an SRS RAI policy to a run by
  its `_id` (copyable from the AgentOS Policies UI). When set it is used
  directly, skipping the AgentOS binding lookup; when omitted, the policy bound
  to `agent_name` in AgentOS is fetched and used. Only `{policyId, principalId}`
  is forwarded — the SRS endpoint/key stay server-side (the CAS fills them).
- **`computeragent.agentos.bind_policy(agent_id, policy_id)`** — attach/detach a
  policy to an agent from the SDK (same write as the UI's Policy tab,
  `PUT /agents/{id}/policy`); pass `None` to detach.

### Changed

- Per-LLM-call OpenTelemetry spans with cache-aware token accounting and
  per-call TTFT; session/call-id correlation threaded through the policy
  pipeline.

## [0.2.1] — 2026-06-08

### Added — single AgentOS config via a discovery document (no endpoint paths in the SDK)

A consumer now gives the SDK **one** AgentOS URL plus its `cak_` key, and the SDK
discovers every other endpoint at runtime — so a server-side route change never
forces an SDK release.

- **`AGENTOS_DISCOVERY_URL`** — the full URL of the AgentOS discovery document
  (e.g. `https://host/agentos/api/discovery`). The SDK GETs it once (memoized)
  and reads the **absolute URLs** for ingest, `agent_id`/git-cred resolve, and
  the OTLP trace gateway straight from it. **No endpoint path is hardcoded or
  constructed in the SDK** — move a route server-side, update its discovery
  document, and existing SDKs pick up the new URLs on next start.
- The discovery document is public (URLs only, no secrets) — the OIDC
  `.well-known` pattern. Server side: new `GET /agentos/api/discovery` in
  `@computeragent/agentos-server` returns `{ version, endpoints }` with absolute
  URLs derived from the request origin (honouring `X-Forwarded-*`, pinnable via
  `AGENTOS_PUBLIC_URL`).
- **Overrides** still win for a single endpoint or a discovery-less setup:
  `AGENTOS_INGEST_URL`, `AGENTOS_API_URL`, `AGENTOS_OTEL_URL` (full URLs).
- The remote-harness URL gained an env fallback, `COMPUTERAGENT_HARNESS_URL`
  (the `harness_url=` kwarg still wins) — the harness/CAS is a separate service,
  always a full URL, never discovered.
- The one `cak_` key (`COMPUTERAGENT_HARNESS_TOKEN`, legacy `AGENTOS_INGEST_TOKEN`
  honoured) now authenticates ingest + every resolved endpoint, including the
  ingest sink's bearer.

### Added — remote harness support (`ComputerAgent(harness_url=...)`)

`ComputerAgent` can now run an agent on a **remote harness server** instead of
in-process, speaking the Harness / Computer-Agent Protocol over HTTP. This makes
the SDK a thin, stateless client — engine execution lives on the harness pod.

- `ComputerAgent(harness_url=..., harness="claude-agent-sdk", source=..., harness_kind=...)`:
  - `harness_kind="protocol"` (default) → the bare `/v1/sessions/*` harness server
    (`@computeragent/harness-server`, port 7700). Full multi-turn + `on_tool_call`
    permission round-trip.
  - `harness_kind="server"` → the ComputerAgent server (port 8787): the client picks
    the substrate via `runtime` (`local`/`bwrap`/`e2b`), warm `/sandboxes/:id/chat`
    multi-turn, `/run` one-shot. (No interactive permission endpoint — the server's
    `permission_mode`/SRS policy governs tools.)
  - `harness_kind="auto"` probes `/v1/health` then `/health`.
- `agent.chat()` streams raw `HarnessEvent`s (`ca_session_started`, `sdk_message`,
  `ca_usage_snapshot`, `ca_session_ended`, …); `await handle.result()` returns the
  usual `ChatResult` (messages + usage + session_id). `run_task(harness_url=...)` too.
- New standalone clients (the "talk to the protocol" surface), exported lazily:
  `from computeragent import HarnessClient, ComputerAgentServerClient`.
- New `[remote]` extra (`httpx`). Local in-process mode is unchanged.

### Added — OTel traces via the AgentOS gateway (one key, no vendor creds on the SDK)

`OtelSink` can now export OTLP **through the AgentOS server** instead of straight
to a vendor, so SDK consumers carry only their `cak_` key — the vendor/collector
credential lives solely on the server.

- Set `AGENTOS_OTEL_URL=<host>/agentos/api/otel`; the sink targets it and
  **auto-injects** `Authorization: Bearer <COMPUTERAGENT_HARNESS_TOKEN ||
  AGENTOS_INGEST_TOKEN>`. No `OTEL_EXPORTER_OTLP_HEADERS`, no New Relic key on the
  client. A direct `endpoint=`/`configure()` endpoint or an explicit
  `Authorization` header is respected (not overridden).
- Server side (`@computeragent/agentos-server`): new cak_-authed (fail-closed)
  OTLP reverse proxy `POST /agentos/api/otel/v1/{traces,metrics,logs}` that
  forwards the raw OTLP body verbatim (protobuf/gzip preserved) to
  `OTEL_GATEWAY_UPSTREAM_ENDPOINT` with `OTEL_GATEWAY_UPSTREAM_HEADERS`.
- The SDK's three AgentOS URLs (`AGENTOS_INGEST_URL`, `AGENTOS_API_URL`,
  `AGENTOS_OTEL_URL`) are now all keyed by the single `cak_`.

### Added — `agent_id` as a resolvable handle + mandatory ingest

Building on the stable `agent_id` below, it's now a **handle to a registered
agent** and AgentOS-backed runs are guaranteed to ship telemetry.

- **Run by reference:** `ComputerAgent(agent_id="…")` with **no `source`** fetches
  the agent's `source`/`harness`/`model` from AgentOS
  (`POST {AGENTOS_API_URL}/agents/resolve`, cak_-authed, group-scoped) — you no
  longer repeat them. `source` is now optional (`source` *or* `agent_id`
  required). Explicit `source`/`harness`/`model` override the resolved values; an
  unregistered `agent_id` with no `source` is a hard, explicit error (run-existing
  only — pass `source` to define on first run). New
  `computeragent.harness.agent_resolve_client.resolve_agent`.
- **No event-loss holes:** when `agent_id` is set, an ingest sink is **required**
  — `ComputerAgent` raises at construction if neither `AGENTOS_INGEST_URL` nor an
  explicit `telemetry_pipeline` is configured. Pure-local runs without `agent_id`
  are unaffected.
- Server: new `POST /agentos/api/v1/agents/resolve` (`@computeragent/agentos-server`)
  returns the registry agent's run definition by `agentId`, scoped to the key's group.

### Added — stable `agent_id` (rename-safe registry identity)

`ComputerAgent(agent_id="…")` (or the `COMPUTERAGENT_AGENT_ID` env var) supplies a
**stable agent identifier** decoupled from `agent_name` (the display label), so
renaming an agent no longer re-registers it as a new one in AgentOS.

- New `agent_id` kwarg on `ComputerAgent` + `run_task`; falls back to
  `COMPUTERAGENT_AGENT_ID`, then to legacy name-keyed behavior when unset.
- Stamped onto **every** telemetry event (`TelemetryEvent.agent_id`, serialized
  on the AgentOS ingest wire) via the coordinator's emit chokepoint — both the
  in-process and remote paths. **Telemetry/registry only — never affects
  execution** (decided by `source`) and never sent in the sandbox/session-create
  body.
- Server side (`@computeragent/agentos-server`): when present, the registry +
  per-agent joins (`sessions`/`chat_sessions`/`agent_logs`) key on `agent_id`
  (name becomes a mutable field); a legacy name-keyed doc is adopted, not
  duplicated. Absent → unchanged.

### Added — remote auth (`cak_` API key)

- `ComputerAgent(harness_token="cak_…")` is sent as `Authorization: Bearer` to
  the remote harness / CAS. When omitted it falls back to the
  `COMPUTERAGENT_HARNESS_TOKEN` env var (mirroring how `AgentOSHttpSink` reads
  `AGENTOS_INGEST_TOKEN`). One key serves remote auth, telemetry ingest, and the
  git-credential resolve below.

### Added — private GAP repos (group-scoped git credentials + SHA sync)

The `local` substrate can now clone **private** git repos by resolving a
short-lived PAT from AgentOS, without leaking the token.

- New `computeragent.harness.git_credential_client.resolve_git_credential()`:
  POSTs the repo URL + the SDK's `cak_` key to
  `POST {AGENTOS_API_URL}/git-credentials/resolve`; AgentOS returns the
  decrypted PAT for the key's **group** + the repo **host**. Best-effort — any
  missing config / network error / 401 / 403 / 404 returns `None` and the
  substrate clones unauthenticated (public-repo behavior unchanged). The token
  is never logged.
- `LocalSubstrate.boot(..., git_credential=...)` injects the PAT via
  `GIT_CONFIG_*` → `http.<host>.extraHeader` (Basic), so the token never appears
  in `argv`, the URL, or error/log surfaces (clone errors log host-only).
  `git@`/`ssh://` URLs pass through. `Substrate.boot` + `SubstrateHandle` gain
  the `git_credential` param / `revision` field.
- **SHA sync:** after cloning, the substrate runs `git rev-parse HEAD` and the
  coordinator stamps it onto the manifest (when the loader didn't supply one) so
  it rides `session_started` as `agent_sha` → the server records
  `sourceSha`/`sourceSyncedAt` on the registry doc.
- New env: `AGENTOS_API_URL` (credential-resolve base) + the existing
  `COMPUTERAGENT_HARNESS_TOKEN` (the `cak_` key; role needs `git-credentials:read`).

### Fixed — OTel agent identity on every span

`gen_ai.agent.{name,version,id,description}` are now stamped on **every** span of
a session (the `invoke_agent` root *and* its `chat` / `execute_tool` children),
not just the root. A flat `WHERE gen_ai.agent.name = X` filter now selects the
whole trace; previously any per-agent aggregate over all spans (span count,
latency-by-operation, throughput) silently dropped every child span.

### Changed — AgentOS telemetry now ships over HTTP (**BREAKING**)

The AgentOS Mongo sinks have been replaced by a single HTTP sink. The AgentOS
server now owns the document schema and performs the Mongo writes; the SDK just
POSTs telemetry events to it. This removes the `motor` dependency from the
`[agentos]` extra and decouples the dashboard schema from a Python release.

- **Added** `AgentOSHttpSink` (`computeragent.telemetry.sinks.AgentOSHttpSink`):
  buffers `TelemetryEvent`s and POSTs batches to
  `POST {AGENTOS_INGEST_URL}` (e.g. `…/agentos/api/ingest/events`), with an
  optional `Authorization: Bearer {AGENTOS_INGEST_TOKEN}`. Drains on
  `session_ended`, on a size threshold, and on `flush()`/`aclose()`.
- **Auto-attach** now keys on `AGENTOS_INGEST_URL` (+ optional
  `AGENTOS_INGEST_TOKEN`) instead of `AGENTOS_MONGO_URL` / `AGENTOS_MONGO_DB`.
- **Removed** `AgentRegistrySink` and `MongoMessageSink` (and the `motor` dep).
  The registry/log/session/message projection they performed now lives in the
  AgentOS server (`@computeragent/agentos-server`, `POST /agentos/api/ingest/events`).
  The server additionally writes the canonical `chat_sessions` row so
  library-mode sessions finally appear in the dashboard session list, and no
  longer writes the dead `slack_threads` collection.
- **Migration:** swap `AGENTOS_MONGO_URL`/`AGENTOS_MONGO_DB` for
  `AGENTOS_INGEST_URL` (+ `AGENTOS_INGEST_TOKEN`). Code that constructed
  `AgentRegistrySink`/`MongoMessageSink` directly should use `AgentOSHttpSink`
  (or rely on env auto-attach). Breaking → release as **0.3.0**.

## [0.2.0]

Adds a harness-based agent stack alongside the existing drop-in proxy,
mirroring the TypeScript `ComputerAgent` shape (engine / substrate /
session-store / identity-loader axes). The 0.1.x surface
(`query`, `ClaudeSDKClient`, telemetry pipeline, policy authorizer) is
completely untouched — this is a strictly additive release.

### Added — harness layer

- **Public harness entry points** at the top level: `ComputerAgent`,
  `run_task`, `ChatHandle`, `ChatResult`, `UsageRollup`.
- **Four orthogonal Protocols** in `computeragent.protocol_types`:
  `EngineDriver`, `Substrate`, `SessionStore`, `IdentityLoader`. Plus
  value types: `EngineEvent`, `EngineContext`, `EngineCapabilities`,
  `AgentManifest`, `PermissionRequest`, `SessionStoreEntry`,
  `SubstrateHandle`, `PolicyDecider`, `IdentitySource`,
  `PermissionDecision`.
- **Built-in engines**:
  - `claude-agent-sdk` — wraps `claude_agent_sdk.query()` directly
    (deliberately not the 0.1.x `_proxy.query` — avoids double-emit
    telemetry under the harness).
  - `gitagent` — Python re-implementation of the TypeScript
    `engine-gitagent` loop. **Dispatches on model prefix:**
    - `anthropic:claude-…` / bare `claude-…` → native Anthropic
      Messages API via `ANTHROPIC_API_KEY` (+ optional
      `ANTHROPIC_BASE_URL`).
    - `openai:gpt-…` / bare `gpt-…` / unknown → OpenAI-compatible
      Chat Completions via `GITCLAW_MODEL_BASE_URL` + `OPENAI_API_KEY`.
    Requires the `[gitagent]` extra. **MCP-only tool execution in
    0.2.0** — wire an MCP filesystem server if you need `Read` /
    `Write` / `Bash`; built-in tools land in 0.3.0.
- **Built-in substrate**: `local` — caller-owned path passthrough, with
  `git clone --depth 1` for `https://github.com/…` / `git@…` / `ssh://…`
  URLs (with or without the `.git` suffix) into temp dirs that are
  cleaned up on dispose.
- **Built-in session store**: `memory` — process-local async dict,
  thread-safe via `asyncio.Lock`.
- **Built-in identity loaders** in `computeragent.identity`:
  - `passthrough` — treats `dict` sources as inline manifests,
    `Path` / `str` sources as workspace-name-only.
  - `gap` — Python port of `@open-gitagent/identity-gitagentprotocol`.
    Reads `agent.yaml` from the cloned workspace, stitches `SOUL.md` +
    `RULES.md` + `AGENTS.md` into the system prompt, maps GAP fields
    to engine-specific options (strips `anthropic:` prefix for the
    Claude SDK adapter, keeps the prefix for gitagent's backend
    dispatch). Requires the `[gap]` (or `[gitagent]`) extra for
    `pyyaml`.
- **Plugin registries**: `register_engine` / `register_substrate` /
  `register_session_store` / `register_identity_loader`. Third-party
  plugins drop in by registering a name + a Protocol-satisfying
  instance.
- **New optional extras**:
  - `[gitagent]` → `openai>=1,<2`, `anthropic>=0.40,<1`, `pyyaml>=6,<7`.
  - `[gap]` → `pyyaml>=6,<7` (subset of `[gitagent]`, for GAP-only
    consumers that don't use the gitagent engine).
- **Harness coordinator** in `computeragent.harness.coordinator`
  - One stable `session_id` reused across every `.chat()` call on a
    single `ComputerAgent` instance — multi-turn lands as **one** Mongo
    `sessions` doc with `entries = [user, asst, user, asst, ...]` in
    chronological order instead of N orphan sessions.
  - `session_started` fires once per lifecycle, `session_ended` fires
    once on `dispose()` — **one `agent_logs` row per conversation**,
    not per turn.
  - Per-chat `user_message` events keep `sessions[].entries` ordered
    deterministically (pipeline flush at chat boundary serialises sink
    writes).
  - Synthesises `assistant_message` / `tool_use` / `tool_result`
    `TelemetryEvent`s for non-`claude-agent-sdk` payloads (Anthropic
    response blocks, OpenAI `ChatCompletionMessage`, dict tool-result
    envelopes) so every engine writes to the AgentOS sinks the same
    way.
- **Policy reuse is zero-glue**: the existing `PolicyToolAuthorizer`
  is structurally compatible with the new `PolicyDecider` Protocol.
  Pass `policy_decider=PolicyToolAuthorizer(...)` into
  `ComputerAgent(...)` and the Claude engine wires it into
  `ClaudeAgentOptions.can_use_tool`; the gitagent engine routes every
  tool call through the same slot.

### Fixed — AgentOS Mongo write flow

These fixes apply to BOTH the 0.1.x proxy path and the new harness
path:

- **`MongoMessageSink` is now auto-attached** by
  `telemetry.config._auto_sinks()` whenever `AGENTOS_MONGO_URL` is
  set. Previously only `AgentRegistrySink` auto-attached, so the
  per-event `agent_messages` collection stayed empty unless the
  caller wired the sink explicitly. (qa-worker-demo's existing
  `setup_telemetry()` wiring keeps working unchanged — its manual
  `MongoMessageSink(...)` overrides the auto-attach.)
- **Model name prefix stripped at every Mongo write site.** GAP
  manifests use `model: "anthropic:claude-sonnet-4-5-20250929"` as a
  routing hint, but Claude CLI and downstream consumers reject the
  prefix. `agent_registry.model`, `agent_registry.source.model`,
  `agent_registry.source.manifest.model`, and `sessions.model` all
  land as the bare model id.

### Added — AgentOS UI signalling

- **`source.type = "library"`** for harness-mode registry docs.
  Library-mode agents run in their host process (no remote
  `harness-server` to proxy a chat-sandbox spawn to), so writing a
  source shape that fails AgentOS's `hasResolvableSource()` check is
  the right signal. Combined with the matching AgentOS-side patch
  (a derived `liveChatCapable` field on `GET /agents`), the
  "New chat" button disappears for these agents and the chat-sandbox
  endpoint refuses cleanly with `LIBRARY_AGENT_NO_LIVE_CHAT`. The
  0.1.x drop-in proxy still writes the original `type: "inline"`
  shape so any existing chat-sandbox setup keeps working for those
  callers.

### Two modes

Drop-in (0.1.x, unchanged)::

    from computeragent import ClaudeAgentOptions, query
    async for msg in query(prompt="...", options=ClaudeAgentOptions(...)):
        ...

Harness (new in 0.2.0)::

    from computeragent import ComputerAgent
    async with ComputerAgent(source=Path.cwd()) as agent:
        result = await agent.chat("Hello")
        print(result.messages)

Harness + GAP repo (new in 0.2.0)::

    async with ComputerAgent(
        source="https://github.com/NeuralgoLyzr/lyzr-docs-agent",
        engine="gitagent",
        identity_loader="gap",
    ) as agent:
        result = await agent.chat("In one sentence, what does this agent do?")

### Backwards compatibility

Strictly additive. 0.1.x callers see no behaviour change — `query` /
`ClaudeSDKClient` still go through the proxy and emit telemetry the same
way. New code can opt into the harness; old code keeps running unchanged.

### Out of scope (parked for 0.3.0+)

- HTTP+SSE `harness-server` mode (in-process only in 0.2.0).
- Built-in filesystem tools for `GitAgentEngine` (MCP-only for now).
- Additional engines (`deepagents`), substrates (`e2b`, `vzvm`, `bwrap`),
  session stores (`mongo`, `sqlite`).
- Tool-call risk classification.

## [0.1.3]

`capture_content` now actually captures content. Prior versions accepted
the configuration value and stored it in the module global, but the
``OtelSink`` never read it — no GenAI prompt / completion / tool-content
attributes ever landed on spans.

### Fixed

- ``OtelSink`` now reads ``capture_content`` + ``capture_content_mode``
  (plus the ``COMPUTERAGENT_CAPTURE_CONTENT`` / ``COMPUTERAGENT_CAPTURE_CONTENT_MODE``
  env vars) and emits content per the GenAI semconv on each span:
  - **invoke_agent**: system prompt + initial user prompt (``gen_ai.prompt.0.*``
    + ``gen_ai.prompt.1.*``) and final answer (``gen_ai.completion.final.*``).
  - **chat**: assistant message text (``gen_ai.completion.0.*``).
  - **execute_tool**: tool arguments (``gen_ai.tool.call.arguments.*``) and
    tool result (``gen_ai.tool.call.result.*``).
- All content is truncated to ``COMPUTERAGENT_MAX_ATTRIBUTE_LENGTH`` chars
  (default 8192) so a runaway prompt doesn't blow past OTLP per-attribute
  limits. Middleware (``PiiRedactor`` / ``GuardrailFilter``) still runs
  first — what lands on spans is the redacted projection, never raw.
- ``capture_content_mode``:
  - ``"attributes"`` — content on span attributes (NRQL-queryable).
  - ``"events"`` — ``span.add_event`` per message (preserves history).
  - ``"both"`` — both shapes, for backends migrating from one to the
    other.

### Backwards compatibility

Fully additive. Callers that don't enable ``capture_content`` see the
same span shape they had in 0.1.2 (metadata-only). Callers that already
set the env var were getting nothing on spans before this release —
they'll now start seeing content where they expected it.

## [0.1.2]

Agents can now carry a human-readable **description** alongside the name.
The description flows from `options.agent_description` (opt-in via
setattr, mirrors the existing `options.agent_name` Strategy 1 path)
through every event in the telemetry pipeline and lands on:

* `agent_registry.description` (top-level field) and
  `agent_registry.source.manifest.description` (inline manifest) — so
  AgentOS SPAs that surface the field pick it up automatically.
* The `invoke_agent` OTel span as `gen_ai.agent.description` (a custom
  attribute paired with the existing `gen_ai.agent.name`).
* `agent.yaml` inside the inline IdentitySource — harness clones get the
  description in their workdir, matching the way hosted (git-sourced)
  agents already carry it.

### Added

- `TelemetryEvent.agent_description: str | None` field. Propagated by
  the proxy on every event, not just `session_started`.
- `query.py:_derive_agent_description(options)` helper, mirroring
  `_derive_agent_name`. Returns `None` when no description is set —
  unlike the name, there is no implicit fallback to `system_prompt`
  because a description is a human label.
- `OtelSink` emits the new `gen_ai.agent.description` span attribute
  on `invoke_agent` when the description is non-empty.
- `AgentRegistrySink._upsert_registry` writes the `description` field
  on the registry doc and passes it down into `_inline_source_for(...)`
  → `_build_agent_yaml(...)`.

### Backwards compatibility

Fully additive. Callers that don't set `options.agent_description`
behave exactly as they did in 0.1.1: the `description` field is absent
from the registry doc / manifest / span. The `_inline_source_for` and
`_build_agent_yaml` helpers add an optional `description=""` parameter
with the empty string as default — no caller has to change a thing.

## [0.1.1]

OTel sink now reaches real OTLP backends. Four interlocking bugs in the
0.1.0 sink combined to make `OTEL_EXPORTER_OTLP_ENDPOINT` look configured
while quietly exporting nothing useful — verified against New Relic, fixed
end-to-end.

### Fixed

- `OtelSink()` constructed inside `configure(sinks=[OtelSink(), ...])`
  now falls through to `OTEL_EXPORTER_OTLP_ENDPOINT` /
  `OTEL_EXPORTER_OTLP_HEADERS` / `OTEL_SERVICE_NAME` when the module config
  hasn't been populated yet. The previous order (kwarg → `_CONFIG` only)
  always missed env vars because `_CONFIG` is empty until `configure()`
  finishes, but `configure()` builds its sinks list — running
  `OtelSink.__init__` — first. Net effect was a silent fallback to the
  console exporter.
- `endpoint=` arguments that point at a base URL (e.g. New Relic's
  `https://otlp.nr-data.net`) now have the per-signal path appended
  automatically. The OTel HTTP exporters treat a non-None constructor
  endpoint as the full URL with no path inference, so passing a base URL
  produced 404s. The new `_signal_url(...)` helper appends `/v1/traces` or
  `/v1/metrics` only when the URL doesn't already specify a signal — fully
  backward-compatible for callers who already passed signal-specific URLs.
- Child spans (`chat`, `execute_tool`) now nest under the active
  `invoke_agent` span. The handlers previously called `start_span(...)`
  without a context, so each child became its own root span and the trace
  tree fragmented across multiple `trace_id`s. The new `_parent_ctx(...)`
  helper rebuilds the OTel context from the per-session invoke_agent span
  reference held in `_invoke_spans`.
- Metric histograms now default to **delta** aggregation temporality (was
  cumulative). New Relic / Datadog direct / Honeycomb OTLP intakes all
  require delta — cumulative payloads ingest but render as zero, hiding
  cost / token / latency data. Opt back into cumulative via
  `OtelSink(temporality="cumulative")`,
  `configure(temporality="cumulative")`, or
  `COMPUTERAGENT_OTEL_TEMPORALITY=cumulative` — useful when exporting to a
  collector that aggregates downstream.

### Added

- `OtelSink(temporality="delta" | "cumulative")` constructor parameter +
  matching `configure(temporality=...)` parameter +
  `COMPUTERAGENT_OTEL_TEMPORALITY` env var.

## [0.1.0]

Initial public release. Drop-in for `claude-agent-sdk` with a proxied telemetry
pipeline, policy-based tool authorization, and full AgentOS integration.

### Added — drop-in surface

- Identity-preserving re-export of every public symbol from
  `claude-agent-sdk` (`isinstance(msg, ResultMessage)` works regardless of
  which name space the user imported from). The Claude CLI subprocess,
  AWS Bedrock auth, MCP servers, permission modes — all behave exactly as
  the upstream SDK does.
- `_proxy.query` and `_proxy.ClaudeSDKClient` thin wrappers that tap every
  yielded message into a telemetry pipeline before yielding it unchanged.
  The proxy is a pure tap; sink writes run as background tasks so a slow
  exporter never stalls the agent hot path.
- Stable per-run session id (the proxy's own placeholder) used for all
  sink bookkeeping; upstream's session id surfaced as
  `payload.upstream_session_id` on the `session_ended` event for
  join-by-id.

### Added — telemetry pipeline

- `Pipeline` middleware-then-fan-out with non-blocking sink emission,
  broad exception handling, and a `flush()` that awaits pending tasks at
  session teardown. Telemetry never breaks an agent run.
- `PiiRedactor` middleware — built-in patterns (email, phone, US SSN,
  credit-card-shaped numbers, AWS access keys) + user-supplied extras,
  JSON-path allow/denylists, `mask` / `hash` / `drop` strategies.
  Fail-closed on substitution error.
- `GuardrailFilter` middleware — attribute-length truncation, tool-name
  allowlist (glob-aware: `mcp__*`), per-session cost ceiling with a
  synthesised `budget_exceeded` session-end event, user-supplied content
  filter.
- Event kinds: `session_started`, `user_message`, `assistant_message`,
  `system_message`, `tool_use`, `tool_result`, `usage_snapshot`,
  `policy_decision`, `session_ended`.

### Added — sinks

- `OtelSink` (`pip install 'computer-agent-py[otel]'`) — OpenTelemetry
  spans (`invoke_agent` → `chat` → `execute_tool`) and metrics
  (`gen_ai.client.token.usage`, `gen_ai.client.operation.duration`,
  `computeragent.usage.cost_usd`) following the GenAI Semantic
  Conventions. Attribute keys match the TS `@computeragent/observability`
  package byte-for-byte so polyglot deployments produce one coherent
  trace tree. Configurable via `configure(...)` or env
  (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`).
  Vendor-neutral — New Relic, Datadog, ClickHouse, Honeycomb, Tempo,
  Jaeger all work with a single env-var swap.
- `AgentRegistrySink` (`pip install 'computer-agent-py[agentos]'`) —
  writes to the AgentOS Mongo collections the existing TS SPA already
  reads:
  - `agent_registry` (idempotent upsert per agent name, includes a full
    inline `IdentitySource` with `files: {agent.yaml, CLAUDE.md}` derived
    from `ClaudeAgentOptions` — the AgentOS harness can spin up a live
    chat sandbox from this without git access)
  - `agent_logs` (per-run rollup with truncated query/reply, tokens,
    cost, ok/error)
  - `sessions` (per-session chat-bubble transcript — drives the Chat tab)
  - `slack_threads` (parity row that drives the SPA's per-agent
    `sessionCount` + `lastActivity` aggregates)
  - Writes also a `bot` field on `agent_logs` for TS-side log-count
    aggregation.
  Doc shapes match the TS `@open-gitagent/agent-registry-mongo` package
  exactly. DocumentDB-compatible operators only (`$set`, `$setOnInsert`,
  `$push`, `update_one(upsert=True)`, `insert_one`). Opt-out for sessions
  via `AgentRegistrySink(write_sessions=False)`.
- `MongoMessageSink` (`pip install 'computer-agent-py[agentos]'`) —
  per-message archive into a new `agent_messages` collection. One
  document per non-rollup event (`user_message`, `assistant_message`,
  `tool_use`, `tool_result`, `usage_snapshot`, `policy_decision`,
  `system_message`). PII redaction runs before the archive — the
  collection only ever sees redacted payloads. Configurable per-field
  truncation keeps doc size predictable.

### Added — policy-based tool authorization

- `PolicyEngine` Protocol + `PolicyToolAuthorizer` adapter that
  implements the exact `claude_agent_sdk.types.CanUseTool` signature.
  Drops onto `ClaudeAgentOptions.can_use_tool`; user supplies
  `principal_resolver`, optional `resource_resolver` /
  `context_resolver` (sync or async). Returns
  `PermissionResultAllow` / `PermissionResultDeny` with the engine's
  reason.
- `OpaPolicyEngine` — remote-HTTP backend (Open Policy Agent's
  `POST /v1/data/<path>`). Accepts both bare-boolean and object-form
  responses with optional reason / obligations. Uses `httpx` (already a
  core dep) — no extra to install. Fail-closed by default.
- `CedarPolicyEngine` (`pip install 'computer-agent-py[cedar]'`) —
  in-process Cedar evaluation via the `cedarpy` Rust bindings. Policies
  are compiled at construction; parse errors surface at startup, not at
  first request.
- `policy_decision` telemetry event — `OtelSink` annotates the active
  `execute_tool` span with `policy.decision`, `policy.reason`,
  `policy.engine`, `policy.latency_ms`.

### Added — examples

- `drop_in.py` — a verbatim upstream-SDK snippet, only the
  imports changed.
- `with_otel.py` / `with_new_relic.py` / `with_datadog.py` —
  vendor-neutral OTel destination examples.
- `with_agentos.py` / `with_message_archive.py` — Mongo sink examples.
- `with_pii_redaction.py` — middleware in front of every sink.
- `with_opa_policy.py` / `with_cedar_policy.py` — policy-gated tool use.
- `multi_sink.py` — all sinks + all guardrails together.
- `e2e/run_live_demo.py` — full live demo against the AgentOS
  docker-compose stack (mongo + clickhouse + otel-collector + harness +
  agentos-server + SPA).
- Sample Rego (`examples/policies/computeragent.rego`) and Cedar
  (`examples/policies/computeragent.cedar`) policy files.

### Pinned upstream

- `claude-agent-sdk >= 0.2, < 0.3`

### Verified

- 120+ unit tests pass; ruff/format/mypy clean; wheel + sdist build
  clean. Live e2e verified end-to-end against the AgentOS stack pointing
  at AWS DocumentDB + New Relic — agent registers, runs, logs roll up,
  sessions transcript renders, OTel spans flow, `chat-sandbox` spins up
  a live harness sandbox from the inline source files.

[Unreleased]: https://github.com/open-gitagent/computer-agent-py/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/open-gitagent/computer-agent-py/releases/tag/v0.1.0
