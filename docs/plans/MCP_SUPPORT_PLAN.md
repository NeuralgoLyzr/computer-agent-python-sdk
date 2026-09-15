# MCP support for the Python SDK — implementation plan

**Status:** plan only. No code in this document is meant to be committed as-is; the snippets exist to pin down shapes.

**Scope.** The Python SDK is collapsing to one run mode: given `base_url` (an AgentOS API base, e.g. `http://localhost:8788/agentos/api`), `agent_id` and `api_key` (a Lyzr workspace key sent as `x-api-key`), it does `GET {base}/v1/agents/{id}`, reads the agent doc, and runs the agent **locally, in-process** through `HarnessCoordinator` + `ClaudeAgentEngine` (which wraps `claude_agent_sdk.query()`). MCP was deferred out of that work. This plan is how it comes back.

The agent doc carries:

```json
"mcp_servers": [{ "server_id": "srv_…", "action_names": ["get_weather"], "tool_name": "Weather", "persist_auth": false }],
"mcp_mode": "engine"
```

Both fields are emitted verbatim by AgentOS at `packages/agentos-server/src/routes/agents.ts:713-721` (`mcp_servers` mapped from `agent.mcpToolConfigs`, `mcp_mode` defaulted explicitly rather than omitted).

---

## 1. The TypeScript wire and server side

### 1.1 What a descriptor is (and what it deliberately is not)

`McpDescriptor` is declared twice — once on each side of the wire, deliberately duplicated rather than shared:

- `packages/agentos-server/src/agent-defs.ts:863-868` (producer)
- `packages/computeragent-server/src/index.ts:4193-4202` (consumer)

```ts
export interface McpDescriptor {
  name: string;      // tool NAMESPACE — engine exposes mcp__<name>__<tool>
  serverId: string;  // the Lyzr MCP service's srv_… id
  tools?: string[];  // allowlist; empty/absent ⇒ every tool the server exposes
  apiKey?: string;   // credential forwarded upstream; omitted ⇒ proxy's static token
}
```

**There is no `url` field, and that is the point.** The comment at `agent-defs.ts:858-862` and again at `index.ts:4189-4192` / `4209-4214` says why: only the CAS knows whether the sandbox reaches the proxy on loopback or via `LYZR_PROXY_PUBLIC_URL`, and that knowledge already lives in `redactionProxyBase()` (`index.ts:4138-4142`, default `http://127.0.0.1:${LYZR_PROXY_PORT ?? 8786}`). Putting a URL in the control plane would mean two places to get it wrong, and agentos-server cannot see the per-runtime answer anyway. Same split as `applyRedactionBaseUrl` (`index.ts:4297-4316`).

### 1.2 How descriptors are built (`mcpDescriptorsFor`)

`packages/agentos-server/src/agent-defs.ts:892-932`:

- **Mode gate, line 893:** `if ((agent.mcpMode ?? DEFAULT_MCP_MODE) !== "engine") return []`. A `studio`-mode agent gets no descriptors at all — its tools live on the Studio agent's `tool_configs`, and attaching them here too would double them up.
- **Credential, lines 897-914:** prefer the *live caller's presented credential*; fall back to the effective gateway's stored key (so scheduled runs and evals still reach their tools); with neither, omit the field and log a warning that says the bridge will 401 every call this run. Never throws (lines 888-891) — a binding lookup must not fail a run.
- **Namespace, lines 916-925:** `name: b.toolName`. `toolName` defaults to the `serverId` at bind time (`mcp-binding.ts:157-160`), which guarantees it is stable and distinct per server. `tools: b.actionNames`.

Attached to the dispatch body as `body.mcp` at `agent-defs.ts:983-984` (`sandboxBodyFor`) and `agent-defs.ts:1026-1027` (`runBodyFor`), only when non-empty.

### 1.3 How the CAS materializes them (`applyMcpServers`)

`packages/computeragent-server/src/index.ts:4226-4295`. Mutates `body.options` in place so every existing dispatch-site spread picks it up unchanged (`:4216-4218`).

Per descriptor (`:4258-4286`):

```ts
const tools = (d.tools ?? []).filter(Boolean);
const query = tools.length > 0 ? `?tools=${encodeURIComponent(tools.join(","))}` : "";
servers[d.name] = {
  type: "http",
  url: `${base}/mcp/${encodeURIComponent(d.serverId)}${query}`,   // :4274
  ...(d.apiKey ? { headers: { "x-api-key": d.apiKey } } : {}),     // :4277
  alwaysLoad: true,                                               // :4281
};
if (anthropicShaped) allowed.add(`mcp__${d.name}`);               // :4285
```

Notes that matter for the port:

- **The tool allowlist rides in the query string** (`:4268-4271`) so the bridge stays stateless and holds no per-agent binding of its own.
- **`base` is `redactionProxyBase()`** (`:4233`) — the CAS pod's own proxy, loopback by default.
- **A malformed descriptor is logged loudly and skipped**, never dropped silently (`:4259-4267`).
- **`allowedTools` is only written for Anthropic-shaped engines** (`:4249-4256`, `:4289`). The list is `claude-agent-sdk`, `claude-code`, `deepagents`, plus `undefined` (default). gitclaw is excluded on purpose: its `allowedTools` is a strict exact-match set with no server-level form, so an `mcp__<server>` entry there matches nothing and filters out *every* tool including built-ins — observed as the model flatly refusing the task with nothing logged (`:4237-4248`).

**Resulting shape:** an HTTP MCP server named `<tool_name>`, at `<proxy-base>/mcp/<server_id>?tools=a,b`, authenticated with `x-api-key: <caller key>`, whose tools the engine sees as `mcp__<tool_name>__<action_name>`.

---

## 2. The bridge (`packages/llm-proxy-openai/src/mcp.ts`)

### 2.1 Why it exists

Header comment, `mcp.ts:1-18`: the Lyzr MCP service exposes a **REST facade over MCP**, not the MCP wire protocol —

```
GET  /api/v1/mcp/servers/{id}/tools   → { tools: [{ name, description, input_schema }] }
POST /api/v1/mcp/tools/execute        → { result, success, error }
```

The Claude Agent SDK and gitclaw are native MCP JSON-RPC clients and cannot consume that facade. `McpBridge` speaks real MCP Streamable HTTP on `POST /mcp/<serverId>` and translates. It is hosted *inside* the existing translator proxy (mounted at `proxy.ts:249`, dispatched first at `proxy.ts:259-263`) so it inherits the reachability story the CAS already solved for the redaction proxy. Enabled only when `LYZR_MCP_CLIENT_URL` is set (`index.ts:4995`, `:5075-5090`); unset ⇒ the route 404s and nothing else changes.

Only `tools/*` is bridged. `resources/*` and `prompts/*` are deliberately not exposed (`mcp.ts:16-18`): on a shared agent they ignore `persist_auth` and always use the caller's key, so they can silently fetch nothing while tools work fine.

### 2.2 What it does, method by method

| Method | Location | Behaviour |
| --- | --- | --- |
| path parse | `mcp.ts:84-92` | `/mcp/<serverId>[?tools=a,b]` → `{serverId, tools}`; anything else is not ours |
| non-POST | `mcp.ts:100-104` | 405 |
| notification (no `id`) | `mcp.ts:118-122` | 202, empty body — answering `notifications/initialized` with a result confuses strict clients |
| `initialize` | `mcp.ts:153-177` | echoes the client's `protocolVersion` when it names one, else `2025-06-18` (`:23`); `capabilities: { tools: { listChanged: false } }`; `serverInfo: lyzr-mcp-bridge/1.0.0` |
| `ping` | `mcp.ts:179-180` | `{}` |
| `tools/list` | `mcp.ts:182-199`, `210-248` | GET the facade, rename `input_schema` → `inputSchema` (`:235-239`), cache 60s keyed `serverId::credential` (`:218`, `:43`), **filter by the allowlist AFTER caching** (`:243-247`) so a narrow binding cannot poison the cache for a wider one. No `nextCursor` — the facade returns the whole list |
| `tools/call` | `mcp.ts:250-350` | allowlist re-enforced here too (`:266-272`) — a model that saw a wider list or guessed must not reach an unbound tool; POST `/api/v1/mcp/tools/execute` with `{server_id, tool_name, arguments}` |
| anything else | `mcp.ts:204-206` | `-32601 method not found` |

Two pieces of translation are load-bearing and must survive any port:

1. **Error-channel split** (`mcp.ts:309-334`). The facade collapses "the tool ran and failed" and "protocol fault" into `success:false`. Anything that got an HTTP 200 from the facade is a *tool-level* outcome, so it comes back as a normal result with `isError: true` — which the model can see and work around — not a JSON-RPC error, which it cannot.
2. **Content coercion** (`normalizeContent`, `mcp.ts:362-428`). The facade emits blocks outside MCP's content union, e.g. `{type:"structured", data:{…}}`. Passing them through made the Claude SDK reject the *whole* result with `invalid_union`, so a successful call surfaced as `isError: true` — this hit **every** tool call through the facade. Mapping: known MCP types pass through; `structured` is hoisted to the result's `structuredContent`; anything else is JSON-stringified into a text block rather than dropped.

### 2.3 Auth model, and the 2026-09-10 refactor

`8a0038c` — *refactor(auth): drop SRS_API_KEY and LYZR_MCP_TOKEN for the caller's own key* (Vraj Parikh, Thu 10 Sep 2026).

Before: the bridge had an operator token (`LYZR_MCP_TOKEN`) it could fall back to, and the caller-credential guard was opt-in (`requireCallerCredential`, defaulted from the bind posture at `proxy.ts:238` — loopback ⇒ off). After: **`LYZR_MCP_TOKEN` is gone and the guard is unconditional** (`mcp.ts:63-77`, `124-136`): a request with neither `x-api-key` nor `Authorization: Bearer` is rejected **401** with `an x-api-key or Authorization credential is required`. The constructor parameter is retained only to keep the signature stable.

The reason is stated at `mcp.ts:66-74` and `proxy.ts:244-249`: a remote substrate (e2b / lambda-microvm / firecracker) cannot reach CAS loopback, so the proxy must be bound off-host with a publicly-routable URL — at which point a bridge that supplied its own credential was an **open relay**; anyone who found the URL could drive the org's MCP tools on the operator's key. AgentOS always stamps a per-dispatch caller key into the `mcpServers` headers (`applyMcpServers`, `index.ts:4277`), so legitimate traffic never relied on the fallback.

Credential extraction: `x-api-key` first (the canonical Lyzr header and what AgentOS stamps), then `Bearer` (`mcp.ts:435-441`). It is forwarded verbatim upstream as `x-api-key` (`mcp.ts:352-355`).

**Consequence for this plan:** the Python SDK holds exactly the credential the bridge wants — the workspace `api_key` it already sends as `x-api-key` to `GET {base}/v1/agents/{id}`. No new secret is needed on any option below.

---

## 3. `mcp_mode`: `engine` vs `studio`

`DEFAULT_MCP_MODE = "engine"` — `packages/agentos-server/src/mcp-binding.ts:44`, with the rationale at `:32-43`. Parsed strictly (`parseMcpMode`, `:55-67`): an unrecognised value is a 400, never a silent fallback, because silently treating `"Engine"` as `"studio"` would put the agent's tools in the wrong place.

**Where the tool loop runs.**

- **`engine`** — the harness engine owns the tools. `mcpDescriptorsFor` emits descriptors (`agent-defs.ts:893`), the CAS materializes them into `options.mcpServers`, and the Studio agent's `tool_configs` are explicitly **cleared**: `studioBindings(mode, bindings)` returns `[]` for engine mode (`routes/agents.ts:410-412`). The model interleaves MCP tools with `Bash`/`Read`/`Write` in one plan.
- **`studio`** — the bindings are written to the backing Lyzr Studio agent's `tool_configs` as `tool_source: "mcp"` entries (`lyzr-studio-client.ts:285-307`) and Studio runs the whole tool loop *inside one completion*. The harness sends a prompt and gets back a finished answer.

**Consequences of `studio`, stated plainly** (`mcp-binding.ts:38-42`):

- The harness never sees a tool call. There are **no `execute_tool` spans** — in Python those are minted from observed tool-use blocks at `src/computeragent/telemetry/sinks/otel.py:1053-1066`, and nothing arrives to mint them from.
- **No policy gating and no HITL.** The Python enforcement points are `can_use_tool` and the `PreToolUse` hook wired in `src/computeragent/engines/claude_agent.py:105-128`; both fire per tool call from the local CLI. A Studio-side loop bypasses both entirely.
- Guardrail tool-name allowlists (`src/computeragent/telemetry/middleware/guardrails.py:115-118`) never see a name to match.
- It is also invisible to `on_tool_call` and to budget accounting at tool granularity.

Studio mode only suits enriching a single answer, which is why it is the deliberate opt-in rather than the default.

### Which mode the Python port supports

**`engine` only.** Two reasons, both structural rather than preferential:

1. Everything the Python SDK is for — local execution, `execute_tool` spans, `PolicyToolAuthorizer` / `PreToolUse` enforcement, `on_tool_call` HITL — is exactly what `studio` mode removes. A Python SDK that silently ran in `studio` mode would produce runs that look clean and are entirely ungoverned.
2. `studio` mode is not something the SDK *implements* at all; it is a property of the Lyzr Studio agent the model call goes to. The Python SDK does not call Studio — the agent's LLM traffic goes through whatever gateway the run is configured with. There is nothing for the SDK to do except *not* attach tools.

So: `mcp_mode: "studio"` on a resolved agent must be handled by **refusing the run with a specific, actionable error** rather than by quietly attaching nothing (which would be indistinguishable from a working agent whose model never chose a tool — the exact failure class `mcp-binding.ts` exists to eliminate). See Phase 2.

---

## 4. The Python side today

### 4.1 What `claude_agent_sdk` gives us

`ClaudeAgentOptions.mcp_servers: dict[str, McpServerConfig] | str | Path` (`types.py:1615`). `McpServerConfig` is a union of four TypedDicts (`types.py:602-637`):

- `McpStdioServerConfig` — `command`, `args`, `env`
- `McpSSEServerConfig` — `type: "sse"`, `url`, `headers`
- `McpHttpServerConfig` — `type: "http"`, `url`, `headers`
- `McpSdkServerConfig` — `type: "sdk"`, `name`, `instance` (in-process)

Serialization (`_internal/transport/subprocess_cli.py:307-332`): SDK servers are passed to the CLI with `instance` stripped; **external servers are passed through verbatim**, so untyped extra keys like `alwaysLoad` survive into `--mcp-config` (this is what `gap.py:334` relies on).

In-process servers are built with `create_sdk_mcp_server(name, version, tools)` (`__init__.py:307-347`) and `tool(name, description, input_schema)` (`__init__.py:166-226`). **`input_schema` accepts a raw JSON-Schema dict** — documented at `__init__.py:187` and already relied on in this repo by `gap_tools._input_schema` (`src/computeragent/identity/gap_tools.py:163-182`). A handler is `async def h(args: dict) -> dict` returning `{"content": [...], "is_error"?: bool}`.

### 4.2 What this repo already does with MCP

**In-process, GAP script tools** — `src/computeragent/identity/gap_tools.py`:

- `MCP_SERVER_NAME = "gap_tools"` (`:54`); `load_gap_tools` reads `<workdir>/tools/*.yaml`, splits builtin aliases from script tools (`:75-105`), builds one `create_sdk_mcp_server` (`:103`) and returns the qualified names `mcp__gap_tools__<name>` (`:104`).
- `_build_script_tool` (`:140-161`) is a dedicated function *specifically so each handler closes over its own manifest* — the late-binding trap. The port must do the same for per-tool closures.
- The dict key under `mcp_servers` must equal the server name for the `mcp__<name>__*` prefix to resolve (`:63-68`).

**External HTTP servers from a GAP repo** — `src/computeragent/identity/gap.py:315-341`:

```python
for server_name, cfg in (raw.get("mcp_servers") or {}).items():
    ...
    mcp_servers[server_name] = {"type": "http", "url": url,
                                "headers": headers, "alwaysLoad": True}
    allowed.append(f"mcp__{server_name}")
```

HTTP only, `${VAR}` header substitution via `_substitute_env` (`gap.py:396-398`), and a server-level `mcp__<name>` grant. **This is already the shape `applyMcpServers` produces** — the Python loader and the CAS agree on the wire form, which is what makes option (a) in §5 cheap.

**Injection into a live run** — `src/computeragent/harness/coordinator.py:207-241` (the fork server). This is the template for MCP injection because it already solved the two hazards:

- The caller options overlay is a shallow `dict.update` and would **clobber** the loader's `mcp_servers`; the fork path deep-merges instead (`:224-226`, and the comment at `:206-210`).
- `allowed_tools` is **extended only if it already exists** (`:230-231`) — see §6.

**Engine options filter** — `src/computeragent/engines/claude_agent.py:87-91` keeps only keys that are `ClaudeAgentOptions` dataclass fields. `mcp_servers` and `allowed_tools` both are, so nothing new is needed there.

**Resolver** — the single-run-mode work has landed in the working tree as `src/computeragent/harness/agentos_agent.py` (the old `agent_resolve_client.py` is deleted). Relevant symbols:

- `api_base(base_url)` (`:87-107`) — normalises the three accepted spellings of `base_url` to the `/v1` root.
- `resolve_agent(agent_id, *, base_url, api_key, client=…)` (`:189`) — the `GET {base}/v1/agents/{id}` + `x-api-key` call.
- `ResolvedAgent` (`:52-85`) — a frozen dataclass that deliberately holds **no credential**; it currently carries `agent_id / name / harness / source / model / gateway_* / credential_id / agent_type` and **no MCP fields at all**.
- `UnsupportedAgentError(AgentResolveError)` (`:46-49`) — "resolved fine, but this run mode does not implement that shape; the message always names the offending field".
- `_require_supported(doc, agent_id)` (`:131-167`) — where the refusals live. **The MCP refusal is lines 155-161:**

  ```python
  mcp_servers = doc.get("mcp_servers")
  if isinstance(mcp_servers, list) and mcp_servers:
      raise UnsupportedAgentError(
          f"agent {agent_id}: mcp_servers is non-empty ({len(mcp_servers)} bound) and MCP is not "
          "supported in this run mode. The servers would be silently dropped, so the run is "
          "refused instead.")
  ```

  Note the reasoning is already the right one — *refuse rather than silently drop* — so Phase 1 is narrowing this refusal, not reversing its principle. `mcp_mode` is not read anywhere yet.

Consumed in `ComputerAgent.__init__` (`src/computeragent/harness/__init__.py:202-203` for the `base_url` / `api_key` kwargs, `:234-241` for `self._api_key` / `self._api_base` / `self.agent`). The existing refusal is covered by `tests/test_agentos_agent_resolve.py:166-171`, which Phase 1 must rewrite rather than delete.

### 4.3 `claude-agent-sdk` 0.2.140 (MCP 2.x)

`uv.lock:353-354` pins `claude-agent-sdk 0.2.87`; `pyproject.toml` allows `>=0.2,<0.3`. Upstream 0.2.140 widened the `mcp` dependency to `>=1.23.0,<3.0.0` and **replaced the hand-rolled JSON-RPC dispatch for in-process servers with `mcp`'s own in-memory transport**, so resources, prompts and the full set of result content types now reach the CLI; tool cancellation on interrupt also gained mcp-2.x support. (0.2.96 had previously *pinned* `mcp<2.0.0`.)

**Effect on this plan:** material but not blocking, and it argues *for* the in-process design.

- The bridge's hardest-won behaviour (`normalizeContent`, `mcp.ts:362-428`) exists because non-union content blocks got the whole result rejected. On 0.2.140 the in-process path uses `mcp`'s own transport, so returning well-formed blocks is on firmer ground and `structuredContent` has a real path to the CLI. On 0.2.87 it does not — so we normalise to `{"type":"text"}` blocks defensively regardless of version.
- **Action:** raise the floor to `claude-agent-sdk>=0.2.140,<0.3` in `pyproject.toml` as part of Phase 3, and add a `tests/test_optional_extras.py`-style guard that the installed version supports what we assume. Do **not** depend on `mcp` directly; it stays a transitive dependency of the SDK.

---

## 5. The decision: where does the bridge live?

The Python SDK runs locally with no CAS, so there is no bridge in front of it. The Lyzr MCP service is a REST facade; the CLI is a native MCP client; something has to translate.

### Option (a) — require a bridge URL passed at init

`ComputerAgent(..., mcp_bridge_url="https://proxy.example/…")`, and each binding becomes `{"type": "http", "url": f"{base}/mcp/{server_id}?tools=…", "headers": {"x-api-key": api_key}, "alwaysLoad": True}` — byte-for-byte what `applyMcpServers` emits (`index.ts:4272-4282`).

- **For:** ~20 lines. Exactly the precedent the SDK already sets for `llm_proxy_url` and `git_token` — anything that is server-side today gets passed in. Zero divergence risk: one bridge implementation, in TypeScript, shared by the CAS and the SDK. Version skew in the facade is fixed once, server-side.
- **Against:** it only works if an operator publicly exposes the translator proxy. `redactionProxyBase()` defaults to `http://127.0.0.1:8786` (`index.ts:4138-4142`) and the proxy binds loopback by default (`proxy.ts:232`, and the note at `index.ts:4130-4136` that `LYZR_PROXY_PUBLIC_URL` alone is insufficient without `LYZR_PROXY_HOST=0.0.0.0`). So the common local case — laptop, `base_url=http://localhost:8788/agentos/api` — has nothing to point at, and making it work means exposing the *LLM and redaction* proxy to the network, a posture change well beyond MCP. It also reintroduces a second required URL into a run mode whose entire premise is `base_url` + `agent_id` + `api_key`.

### Option (b) — port the bridge into Python

- **For:** no extra infrastructure and no second URL; works on a laptop.
- **Against (naive reading):** running an HTTP JSON-RPC listener inside the SDK (port selection, lifecycle, bind posture, its own 401 guard) is a lot of machinery — and re-creates the exact open-relay hazard `8a0038c` closed.

**But the naive reading is wrong for this SDK.** The CAS needs the bridge to be *HTTP* because the consumer is a Claude CLI process in a sandbox that can only be handed a URL. The Python SDK's consumer is `claude_agent_sdk` in the same process, which accepts an **in-process** server (`McpSdkServerConfig`). So "port the bridge" here means: at boot, call the facade's `GET /api/v1/mcp/servers/{id}/tools`, synthesise one `@tool`-decorated async handler per allowed action that `POST`s `/api/v1/mcp/tools/execute`, and register them via `create_sdk_mcp_server`. **No listener, no port, no URL, no inbound auth surface at all** — the open-relay shape is structurally absent because nothing is listening. The JSON-RPC half of `mcp.ts` (lines 94-208, ~110 lines) disappears; only the translation half (`listTools`, `callTool`, `normalizeContent`, ~120 lines) is ported.

### Option (c) — directly-reachable HTTP servers only; refuse Lyzr-bound ones

- **For:** nearly free — `gap.py:315-341` already does it.
- **Against:** it does not implement the feature. Every binding in an agent doc is a Lyzr `srv_…` id behind the REST facade; there is no URL to reach directly. This option supports a set of agents that is currently empty.

### Decision

**Option (b), implemented as an in-process SDK MCP server**, with **(a) retained as an explicit override** and **(c) kept as the already-shipping GAP capability**.

Rationale, in priority order:

1. It is the only option that works in the run mode being built. No second URL, no infrastructure, no network posture change.
2. It is the only option that gives every MCP call an `execute_tool` span (`otel.py:1053-1066`) and routes it through `can_use_tool` *and* the `PreToolUse` hook (`claude_agent.py:105-128`) — which is the entire argument for `engine` mode existing (`mcp-binding.ts:34-43`). An HTTP bridge would give this too, but (b) gives it without the deployment prerequisite.
3. The security property that motivated `8a0038c` is preserved by construction: the credential is the caller's own `api_key`, there is no operator token, and there is no inbound surface to leave unauthenticated.

**Honest cost of (b):** a second implementation of the facade translation, in a second language, that can drift from `mcp.ts`. Mitigations, which are non-optional parts of Phase 3:

- Port `normalizeContent` (`mcp.ts:362-428`) and the error-channel split (`mcp.ts:309-334`) **behaviourally verbatim**, with the TS file and line cited in the Python docstring.
- Mirror the tool-list cache semantics (60s TTL, keyed by `server_id::credential`, filter after caching — `mcp.ts:218`, `:243-247`).
- A parity test file in the style of `tests/test_gap_loader_parity.py`, asserting the same input → same normalised output for the block shapes `mcp.ts` calls out (`structured`, unknown type, bare string, `null`).

**Why (a) is still kept:** one kwarg, zero maintenance, and it is the right answer for a deployment that *does* front the SDK with the CAS proxy and wants a single audited bridge. It also gives a fast escape hatch if the facade changes and the Python port lags. It is an override, not the architecture.

**The refusal must be relaxed.** Per the task brief the single-run-mode resolver currently refuses any agent whose `mcp_servers` is non-empty. Shipping this means turning that refusal into: accept when `mcp_mode == "engine"` and MCP support is configured; refuse with a distinct message when `mcp_mode == "studio"`; refuse with a distinct message when engine-mode bindings exist but no MCP service base is configured (never run silently tool-less — that is `mcp-binding.ts`'s whole thesis).

---

## 6. Tool naming and allowlists

**Per-engine grant spelling** — `packages/harness-server/src/services/worker-mcp.ts:68-98`:

| Engine family | Tool name | Server-level grant | Source |
| --- | --- | --- | --- |
| Anthropic-shaped (`claude-agent-sdk`, `claude-code`, `deepagents`) | `mcp__<server>__<tool>` | `mcp__<server>` — one entry covers the server | `worker-mcp.ts:85`, `applyMcpServers` `index.ts:4285` |
| gitagent / gitclaw | `<server>__<tool>` | none — `allowedTools` is an exact-match `Set`, no globs | `worker-mcp.ts:86`, and the warning at `:76-82` |

For gitclaw the tool names have to be enumerated individually, and they come from the materialized URL's `?tools=` query (`boundToolNamesOf`, `worker-mcp.ts:89-98`), which is the authoritative per-agent binding. When the query is absent (⇒ every tool) there is nothing to enumerate and the code warns rather than guessing (`:178-189`).

**For the Python port:** `ClaudeAgentEngine` is Anthropic-shaped, so grants are `mcp__<tool_name>`. `GitAgentEngine` is MCP-stub in 0.2.x (`src/computeragent/engines/git_agent.py:20-23`, `:149`) — the plan attaches MCP for `claude-agent-sdk` only and logs a warning for any other engine, matching `applyMcpServers`'s posture of never writing an Anthropic-shaped allowlist for an engine that cannot read it.

**The rule: an `allowed_tools` list is EXTENDED, never CREATED.** `worker-mcp.ts:164-193` states it and this repo already implements it at `coordinator.py:230-231`:

```python
if opts.get("allowed_tools"):
    opts["allowed_tools"] = list(opts["allowed_tools"]) + list(tool_names)
```

Creating a list where none existed flips "every tool allowed" into "only these MCP tools allowed" and silently strips `Read`/`Write`/`Bash`. This was measured, not theorised: on gitagent, a roster entry with `tools: ["write","read"]` produced workers that reported `NO_DEEPWIKI_TOOL`, made **zero** bridge calls, and still came back `completed` under a `pass` scorecard (`worker-mcp.ts:167-174`). Conversely, extending is done for *every* engine, because a model-authored list must be allowed to narrow the built-ins it was given but must not hand back a grant the configuration made (`worker-mcp.ts:174`).

**Namespace collisions.** `tool_name` is a display label that doubles as the namespace (`agent-defs.ts:917-920`); it defaults to `server_id`, which guarantees uniqueness, but an operator can set two bindings to the same `tool_name`. AgentOS validates action-name uniqueness *across* bindings (`mcp-binding.ts:139-152`) but not `tool_name` uniqueness. The Python builder must therefore not silently let one binding's dict key overwrite another's — see Phase 3.

Reserved names to avoid colliding with: `gap_tools` (`gap_tools.py:54`) and `fork` (`coordinator.py:225`).

---

## 7. Phased implementation plan

Each phase is independently shippable and testable. File paths are relative to the repo root.

### Phase 1 — carry the fields (no behaviour change)

**Goal:** the agent doc's MCP fields reach the harness as typed values, and the blanket refusal becomes a targeted one. Nothing attaches tools yet.

- New module `src/computeragent/mcp/binding.py`, called from `src/computeragent/harness/agentos_agent.py`, parsing `mcp_servers` and `mcp_mode` into frozen values:

  ```python
  @dataclass(frozen=True)
  class McpBinding:
      server_id: str
      action_names: tuple[str, ...]
      tool_name: str          # defaults to server_id, per mcp-binding.ts:157-160
      persist_auth: bool

  DEFAULT_MCP_MODE = "engine"   # mirrors mcp-binding.ts:44
  ```

  Parse rules mirrored from `mcp-binding.ts:90-165`: non-list ⇒ error; entry missing `server_id` ⇒ error; empty `action_names` ⇒ error (`MCP_ACTIONS_REQUIRED` — an empty whitelist silently exposes no tools); duplicate action name across bindings ⇒ error (`MCP_DUPLICATE_ACTION`). `mcp_mode` other than `engine`/`studio` ⇒ error, never a silent fallback (`mcp-binding.ts:55-67`).
- Narrow the blanket refusal at `agentos_agent.py:155-161` into three distinct outcomes, keeping `UnsupportedAgentError` and its "name the offending field" contract (`:46-49`): `studio` ⇒ refuse (Phase 2 message); `engine` + no MCP configuration ⇒ refuse with the "configure a Lyzr MCP base or pass `mcp_bridge_url=`" message; `engine` + configured ⇒ proceed (still a no-op this phase, behind a log line). Parsing lives in `binding.py`; `_require_supported` keeps only the policy decision, matching how it already delegates nothing else.
- Add `mcp_bindings: tuple[McpBinding, ...]` and `mcp_mode: str` to `ResolvedAgent` (`agentos_agent.py:52-85`), populated in `resolve_agent` (`:189`). The dataclass holds no credential by design — keep it that way; the `api_key` stays on `ComputerAgent` (`harness/__init__.py:234`) and is passed to the MCP client at boot, never stored on `ResolvedAgent`.
- Thread bindings + mode from `ComputerAgent.__init__` (`src/computeragent/harness/__init__.py:234-241`) down to `HarnessCoordinator.__init__` (`src/computeragent/harness/coordinator.py`) as private fields, alongside the existing `_fork_mode` / `_fork_config` pattern.

**Ships:** clearer errors than today. **Testable:** pure parsing + `respx` on the agent-doc GET; rewrite `tests/test_agentos_agent_resolve.py:166-171` into the three cases rather than deleting it.

### Phase 2 — `studio` mode refusal

**Goal:** an agent that would run ungoverned never runs.

- A dedicated subclass of `UnsupportedAgentError` (`agentos_agent.py:46-49`) — e.g. `McpModeUnsupportedError` — raised from `_require_supported` (`:131-167`) alongside the harness/gateway/orchestration refusals, so it inherits the existing "resolved fine, but this run mode does not implement that shape, and the message names the field" contract. Message names the cause and the fix: *"agent `<name>` is configured `mcp_mode: studio` — the tool loop runs inside Lyzr Studio, so this SDK never sees the tool calls: no `execute_tool` spans, no policy decisions, no HITL prompts. Re-register the agent with `mcp_mode: engine` (the AgentOS default) to run it here."*
- Do **not** offer a `force` flag. Running tool-less under a `studio` binding is indistinguishable from a working agent whose model chose no tool; that ambiguity is the failure class `mcp-binding.ts` exists to remove.

**Ships:** independently. **Testable:** one unit test per mode, next to the existing refusal tests in `tests/test_agentos_agent_resolve.py`.

### Phase 3 — the in-process Lyzr MCP bridge

**Goal:** engine-mode bindings become callable tools.

New package `src/computeragent/mcp/`:

| File | Contents |
| --- | --- |
| `binding.py` | Phase 1 dataclass + parser |
| `lyzr_client.py` | `LyzrMcpClient` — the ported translation half of `mcp.ts`. Two methods over `httpx.AsyncClient`, both sending `x-api-key: <api_key>` (`mcp.ts:352-355`): `list_tools(server_id) -> list[ToolSpec]` (`GET /api/v1/mcp/servers/{id}/tools`, `input_schema` → JSON Schema, 60s TTL cache keyed `f"{server_id}::{api_key}"`, filter after caching — `mcp.ts:210-248`) and `execute(server_id, tool_name, arguments) -> dict` (`POST /api/v1/mcp/tools/execute`) |
| `content.py` | `normalize_content(result) -> tuple[list[dict], Any \| None]` — a behavioural port of `normalizeContent` (`mcp.ts:362-428`), with the TS citation in the docstring |
| `server.py` | `build_lyzr_mcp_servers(bindings, client) -> LyzrMcpResult` — per binding, one `create_sdk_mcp_server(name=tool_name, tools=[...])` plus the `mcp__<tool_name>` grant |

`server.py` shape (illustrative only):

```python
def _build_tool(client, server_id, spec):           # own function per tool —
    async def handler(args):                        # closure late-binding trap,
        out = await client.execute(server_id, spec.name, args or {})   # cf. gap_tools.py:140-146
        if out.get("success") is False:             # tool ran and failed → model-visible
            return {"content": [{"type": "text",
                                 "text": out.get("error") or "tool execution failed"}],
                    "is_error": True}               # mcp.ts:318-334
        content, structured = normalize_content(out.get("result"))
        return {"content": content, "is_error": False}
    return tool(spec.name, spec.description or f"Lyzr MCP tool: {spec.name}", spec.input_schema)(handler)
```

Behaviours that must be carried over, each with its TS citation in a comment:

- Allowlist enforced **both** at list time and at call time (`mcp.ts:243-247`, `:266-272`). In-process this is mostly list-time, but the call handler still rejects an out-of-binding name — the model can guess.
- A transport failure is an `is_error` result, not an exception that kills the turn (`mcp.ts:298-307`).
- Never throw out of boot: a facade outage degrades the run to "no MCP tools this turn", mirroring `mcpDescriptorsFor`'s never-throws rule (`agent-defs.ts:888-891`). Log at WARN naming the server ids.
- Discovery result of zero tools is logged with the requested action names next to the server's real tool names — the single most common cause of a bound tool never appearing (`mcp.ts:188-192`).

Wiring, in `HarnessCoordinator._ensure_booted` (`src/computeragent/harness/coordinator.py`), **immediately after the fork block at `:207-241`** and using the same deep-merge, for the same reason (`:206-210`):

```python
mcp = dict(opts.get("mcp_servers") or {})
for name, server_cfg in built.servers.items():
    if name in mcp:                       # gap_tools / fork / a duplicate tool_name
        logger.warning(...); continue     # never silently overwrite
    mcp[name] = server_cfg
opts["mcp_servers"] = mcp
if opts.get("allowed_tools"):             # EXTEND ONLY — worker-mcp.ts:164-193
    opts["allowed_tools"] = list(opts["allowed_tools"]) + built.grants
```

Also in this phase: raise the `claude-agent-sdk` floor to `>=0.2.140,<0.3` in `pyproject.toml` and refresh `uv.lock` (§4.3). Add an optional extra `mcp = ["httpx>=0.27"]` and fold it into `all`, matching how `agentos`/`remote` are declared.

**Ships:** the feature. **Testable:** entirely offline (stub client) plus `respx` for `LyzrMcpClient`.

### Phase 4 — governance verification

**Goal:** prove the thing that justifies `engine` mode.

No new production code expected; this phase is tests plus whatever small fixes they force.

- An MCP tool call produces an `execute_tool <name>` span with `gen_ai.tool.name == "mcp__<server>__<tool>"` (`otel.py:1053-1066`).
- A `policy_decider` that denies `mcp__*` blocks the call under `permission_mode="default"` (via `can_use_tool`) **and** under `bypassPermissions` (via the `PreToolUse` hook at `claude_agent.py:118-128`).
- `on_tool_call` receives the MCP tool and its arguments.
- The guardrails middleware's `mcp__*` glob (`guardrails.py:115-118`) matches the generated names.

### Phase 5 — escape hatches

- **(a) `mcp_bridge_url=`** on `ComputerAgent.__init__`, next to the existing `harness_url` / `harness_token` kwargs (`src/computeragent/harness/__init__.py:192-200`). When set, skip the in-process builder and emit CAS-identical entries:

  ```python
  {"type": "http",
   "url": f"{base}/mcp/{quote(b.server_id)}?tools={quote(','.join(b.action_names))}",
   "headers": {"x-api-key": api_key},
   "alwaysLoad": True}
  ```

  — byte-for-byte `index.ts:4270-4282`. Grants stay `mcp__<tool_name>` (`:4285`). Document that the bridge 401s without the header (`mcp.ts:124-136`) and that the URL must be the proxy's *externally reachable* origin, not the CAS loopback (`index.ts:4130-4142`).
- **(c) direct HTTP servers** already work through `gap.py:315-341` for GAP repos; no change beyond making sure the Phase 3 merge does not clobber them (it does not — it skips existing keys).

### Phase 6 — documentation

- `docs/ARCHITECTURE.md` — extend the GAP row at `:178` and add an MCP section describing the two paths and the `engine`-only rule.
- `user-documentation.md` — how to configure the Lyzr MCP base, what `mcp_mode: studio` means and why it is refused, and the extend-never-create allowlist rule for anyone passing `options={"allowed_tools": [...]}`.
- `CHANGELOG.md` — note the `claude-agent-sdk` floor bump as a breaking-ish dependency change.

---

## 8. Test plan

Conventions taken from the repo: `pytest` with `asyncio_mode = "auto"` and `--strict-markers` (`pyproject.toml` `[tool.pytest.ini_options]`), `respx` + `httpx.Response` for HTTP (`tests/test_agentos_discovery.py:44-53`, and the agent-doc mocking in `tests/test_agentos_agent_resolve.py`), the `integration` marker for anything needing a live service, shared fixtures in `tests/conftest.py`, and the in-process-MCP assertion style of `tests/test_gap_tools.py:35-54` (assert on `result.mcp_server["type"] == "sdk"` and `["name"]`, and on the qualified `mcp__…` names).

**Fixtures in `tests/conftest.py`:**

- `agent_doc` — **already exists** (`tests/conftest.py:156-163`, deep-copying `tests/fixtures/agent_doc.json`). Tests override `doc["mcp_servers"]` / `doc["mcp_mode"]` in place, exactly as `tests/test_agentos_agent_resolve.py:166-171` already does. Add `mcp_servers: []` and `mcp_mode: "engine"` to the JSON fixture so the default doc matches what AgentOS actually emits (`routes/agents.ts:713-721`).
- `stub_lyzr_mcp` — an in-memory `LyzrMcpClient` double recording `list_tools` / `execute` calls and returning scripted payloads, so `server.py` tests need no network.

**`tests/test_mcp_binding.py`** (pure, no I/O) — parity with `mcp-binding.ts:90-165`: absent vs `[]` (leave-alone vs explicit clear); non-list rejected; missing `server_id` rejected; empty `action_names` rejected; duplicate action across two bindings rejected; `tool_name` defaults to `server_id`; `persist_auth` strictly `is True`; `mcp_mode` unknown value rejected; `mcp_mode` absent ⇒ `"engine"`.

**`tests/test_mcp_lyzr_client.py`** (`respx`) — `list_tools` maps `input_schema` → `inputSchema`-equivalent JSON Schema and filters by the allowlist; the cache serves a second call without a second request (`route.call_count == 1`, as in `test_agentos_discovery.py:53`) and two different credentials do **not** share a cache entry; a non-2xx raises the typed error; `execute` sends `{server_id, tool_name, arguments}` and `x-api-key`.

**`tests/test_mcp_content.py`** (pure) — the parity table from `mcp.ts:362-428`: `None` ⇒ `[]`; a bare string ⇒ one text block; a known block type passes through untouched; `{"type":"structured","data":…}` is hoisted out of `content`; a second `structured` block is ignored, not overwritten (`mcp.ts:413-414`); an unknown type is JSON-stringified into a text block, never dropped; a non-object element is stringified.

**`tests/test_mcp_server_build.py`** (stub client) — one binding ⇒ one `type: "sdk"` server keyed by `tool_name`, grants `["mcp__<tool_name>"]`, and per-action `SdkMcpTool`s; a facade failure at boot yields zero servers and no exception; `success: False` from `execute` becomes `{"is_error": True}` with the upstream message; a transport exception becomes `is_error`, not a raise; **each handler closes over its own tool** (build two tools, call the first, assert the second's name was not used — the `gap_tools.py:140-146` trap).

**`tests/test_mcp_coordinator_wiring.py`** (in the style of `tests/test_fork.py:130-174`) — MCP servers land under `mcp_servers` alongside `gap_tools` and `fork` without clobbering either; a pre-existing key of the same name is skipped with a warning; an existing `allowed_tools` is extended; **an absent `allowed_tools` stays absent** (the single most important assertion in this file — `worker-mcp.ts:164-193`); nothing is attached for `mcp_mode: "studio"` because construction already raised; nothing is attached for a non-`claude-agent-sdk` engine.

**`tests/test_agentos_agent_resolve.py`** (extend in place) — replace the single `test_refuses_bound_mcp_servers` (`:166-171`) with: `mcp_mode: "studio"` raises `McpModeUnsupportedError`; engine-mode bindings with no MCP base configured raise the configuration error naming `mcp_servers`; engine-mode + configured populates `ResolvedAgent.mcp_bindings` with the parsed tuple; an empty/absent `mcp_servers` resolves exactly as today (regression guard for the relaxed refusal); `ResolvedAgent` `repr()` still contains no credential.

**`tests/test_mcp_policy.py`** (Phase 4; uses `upstream_query_factory` / `fake_upstream_client` from `conftest.py:68-145`) — deny on `mcp__*` under both `default` and `bypassPermissions`; an `execute_tool` span is emitted for an MCP tool; `on_tool_call` sees the qualified name.

**`tests/test_mcp_integration.py`** — `@pytest.mark.integration`, skipped unless `LYZR_MCP_CLIENT_URL` and a workspace key are present: list the real tools of a known `srv_…`, execute one, assert a well-formed MCP result. This is the only place the real facade is touched.

**Also:** extend `tests/test_surface_parity.py` / `tests/test_optional_extras.py` for the new `mcp` extra, and guard the `claude-agent-sdk>=0.2.140` assumption there rather than letting a stale lock fail mysteriously at run time.

---

## 9. Open questions — things I could not determine

1. **The single-run-mode work is uncommitted.** It exists in the working tree of `feat/agentos-single-run-mode` (`src/computeragent/harness/agentos_agent.py` added, `agent_resolve_client.py` / `cas_client.py` / `remote.py` / `protocol.py` / `fork.py` deleted) but is not committed, so every line number in §4.2 and §7 is against an in-flight tree and will move. **Phase 1 must re-read `agentos_agent.py` before editing it.** Two consequences worth flagging: `fork.py` is deleted, so the `mcp_servers["fork"]` deep-merge precedent cited at `coordinator.py:207-241` may no longer exist to copy — check whether `_ensure_booted` still has a deep-merge site or whether Phase 3 has to introduce the first one; and `ResolvedAgent` currently carries no MCP fields at all, so Phase 1 is additive to a dataclass whose "holds no credential" invariant (`agentos_agent.py:57-61`) must be preserved.
2. **Where does the Python SDK learn the Lyzr MCP service base?** The AgentOS discovery document publishes only `ingest`, `api`, `otel` (`packages/agentos-server/src/routes/discovery.ts:30-34`) — there is no `mcp` endpoint. Three candidates, none decided: a new discovery key (needs a server change, but matches the SDK's no-baked-paths rule at `src/computeragent/agentos.py:1-20`); a `LYZR_MCP_CLIENT_URL` env var (matches the CAS, `index.ts:4995`); or an explicit init kwarg. A server-side discovery key is the best fit for the SDK's stated design, and is the largest single unknown in this plan.
3. **Does the workspace `api_key` authenticate against the MCP facade?** The bridge forwards the caller's key verbatim to `LYZR_MCP_CLIENT_URL/api/v1/mcp/*` (`mcp.ts:352-355`), while AgentOS's own validation uses a *different* host and path — `/v3/tools/mcp/servers` on the Studio host (`lyzr-studio-client.ts:218-247`, with the note at `:223-226` explaining the choice). Whether one `x-api-key` works against both, and whether a workspace key differs from whatever the CAS forwards in practice, is unverified here. **Verify against a live service before Phase 3 is called done.**
4. **`persist_auth` appears inert under `engine` mode.** It is parsed (`mcp-binding.ts:161`), stored, echoed (`routes/agents.ts:717`) and warned about for group-owned agents (`routes/agents.ts:423-436`), but nothing in `mcpDescriptorsFor`, `applyMcpServers` or `mcp.ts` reads it — the descriptor type has no such field (`agent-defs.ts:863-868`). It presumably matters only inside Lyzr's own service, keyed off the server registration. The plan therefore parses and carries it and does nothing with it. **Unconfirmed.**
5. **Pagination and rate limits on the facade.** `mcp.ts:193-195` asserts the facade returns the whole tool list in one response with no cursor, but that is an assertion about today's service, not a documented contract. Unknown for `tools/execute` rate limits.
6. **`alwaysLoad` on the Python path.** The CAS sets it (`index.ts:4281`) and `gap.py:334` copies it, and `subprocess_cli.py:311-326` passes extra keys through for non-SDK servers — but whether the CLI honours it identically on the Python SDK path, and whether it means anything for an *in-process* server, is unverified. Harmless either way.
7. **`mcp` 2.x transitively.** 0.2.140 widens to `mcp>=1.23.0,<3.0.0`; whether a resolved `mcp` 2.x changes anything observable for in-process servers built by `create_sdk_mcp_server` on this repo's code paths has not been tested. Phase 3's version guard is the mitigation, not an answer.
8. **Duplicate `tool_name` across bindings.** AgentOS enforces action-name uniqueness across bindings (`mcp-binding.ts:139-152`) but not `tool_name` uniqueness, and in the CAS a duplicate would silently overwrite a key in `servers` (`index.ts:4272`). The plan makes the Python side skip-and-warn instead. Whether AgentOS should reject it at bind time is a server-side question raised, not answered, here.
9. **gitagent.** `GitAgentEngine` is MCP-stub in 0.2.x (`src/computeragent/engines/git_agent.py:20-23`, `:260`, `:384`), so the `<server>__<tool>` grant shape from `worker-mcp.ts:86` has no Python consumer yet. Documented in §6 for when it does; not implemented.
