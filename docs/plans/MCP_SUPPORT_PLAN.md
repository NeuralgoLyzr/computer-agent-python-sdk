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

## 0. Revision — 2026-09-16, after re-reading both codebases

The plan above was written before the single-run-mode work landed. §1–§6 (the TypeScript
survey) were re-verified line by line and are **unchanged and accurate**. What follows is
what re-reading turned up; §7 onward is rewritten against it.

### 0.1 Four things in `claude-agent-sdk` that make "port `mcp.ts` verbatim" wrong

This is the substantive finding. The TS bridge emits MCP over the wire, where the full content
union and `structuredContent` are available. The Python SDK's in-process path is **narrower
than the MCP wire**, and every place it is narrower it discards silently — the exact failure
class `normalizeContent` exists to prevent.

Read from the installed `claude_agent_sdk` 0.2.153:

| # | Upstream behaviour | Where | Consequence for a verbatim port |
|---|---|---|---|
| 1 | `run_tool` builds `CallToolResult` from **only** `content` and `isError` | `__init__.py:608-613` | `structuredContent` **has no path to the CLI**. Hoisting a `{type:"structured", data}` block out of `content` — which is what `mcp.ts:413-414` does — would *delete the tool's payload*. Strictly worse than the TS. |
| 2 | `_convert_tool_content` accepts `text`, `image`, `resource_link`, `resource`; **everything else is dropped with a warning** | `__init__.py:_convert_tool_content` | `mcp.ts`'s `MCP_CONTENT_TYPES` also contains **`audio`**. Passing an audio block through, as the TS does, hands it to a converter that drops it. |
| 3 | `_build_input_schema` passes a dict through **only** when it has a string `type` *and* a `properties` key; otherwise it reinterprets the dict as a `{param_name: python_type}` mapping | `__init__.py:_build_input_schema` | A facade schema of `{"type":"object"}` (no `properties`) is not passed through — it is read as a parameter map, producing `{"type":"object","properties":{"type":{...}},"required":["type"]}`. Every call then fails argument validation. |
| 4 | Arguments are validated with `jsonschema` **before** the handler runs; a failure returns `is_error` without ever calling the tool | `__init__.py:602-606` | Validation moves from the remote MCP server to our process. A facade schema stricter or more malformed than what the server actually enforces rejects calls the server would have accepted, and the model sees a tool that never works. |

**Therefore** `content.py` is not a transcription of `normalizeContent`. It is the same
*intent* — never drop what the tool returned — expressed against a different sink:

```
known-and-convertible (text/image/resource_link/resource) → pass through
{type:"structured", data}                                 → JSON text block   (NOT hoisted — 1)
audio, and every other type                               → JSON text block   (NOT passed — 2)
non-object element                                        → str() text block
None/absent                                               → []
```

Same rule in one line: **exactly the four types upstream can convert survive as blocks;
everything else becomes text.** A text block always validates and always reaches the model, so
nothing is lost. The parity test therefore asserts *Python-correct* outputs and cites this
table for the two rows where they deliberately differ from `mcp.ts`.

### 0.2 The MCP base: an init kwarg, and no server change

Open question 2 asked where the SDK learns the Lyzr MCP service base, and floated a discovery
key as "the best fit". Re-reading says otherwise, for a reason that was not visible before:

**AgentOS does not know the MCP base either.** `LYZR_MCP_CLIENT_URL` appears in
`packages/computeragent-server` (the CAS) and nowhere in `agentos-server`; its `config.ts` has
no MCP entry. So a discovery key is not "publish a value the server already holds" — it is a
new config on a service that has never needed one, for a single consumer.

Meanwhile the SDK has an established precedent for exactly this shape: `llm_proxy_url` and
`git_token` are both optional init kwargs, both needed only in specific cases, both things the
server would supply in a hosted deployment. MCP is the third instance of that pattern.

**Decision: `mcp_base_url=` on `ComputerAgent.__init__`**, falling back to
`LYZR_MCP_CLIENT_URL` in the environment (matching the CAS's own var name, so an operator who
has already set it for the CAS gets it for free). No server change. A discovery key stays
available later if a hosted deployment wants one; nothing here forecloses it.

There are **two** reachable spellings of the facade, and the SDK should use the first:

| Base | Paths | Used by |
|---|---|---|
| `LYZR_MCP_CLIENT_URL` (e.g. `https://mcp-dev.test.studio.lyzr.ai`) | `/api/v1/mcp/servers/{id}/tools`, `/api/v1/mcp/tools/execute` | the bridge (`mcp.ts`) |
| `LYZR_UPSTREAM_BASE` (the Studio host) | `/v3/tools/mcp/servers`, `/v3/tools/mcp/servers/{id}/tools` — **reads only, no execute** | AgentOS bind-time validation |

`lyzr-studio-client.ts:223-226` states the two "expose identical bodies for these reads", but
the Studio proxy has no execute route, so only the first can back a running tool. This also
partly answers open question 3: AgentOS reaches the read half with a Studio key on a different
host, which is why one key working against both was never demonstrated. Still to verify live.

### 0.3 `allowed_tools` is inert in this run mode — the real gate is the policy decider

§6 makes "extend `allowed_tools`, never create it" the load-bearing safety rule, and
`tests/test_mcp_coordinator_wiring.py` was to call it "the single most important assertion".

That was written before `build_options` fixed `permission_mode: "bypassPermissions"` as this
run mode's default (`gateway_envs.py:192-194`, mirroring what AgentOS dispatches). Under a
prompt-skipping mode `allowed_tools` grants nothing and forbids nothing — the SDK already says
so out loud at `engines/claude_agent.py:146-152`, and a test pins it.

So the rule stays (it is three lines, and it is correct the moment a caller passes
`permission_mode="default"`), but it is **demoted from safety property to hygiene**. The gate
that actually fires on every MCP call in this run mode is `policy_decider`, routed through the
`PreToolUse` hook at `engines/claude_agent.py:154-172`. Phase 4 is where the feature's
governance claim is proved, and it is no longer optional polish.

### 0.4 `tool_name` becomes an MCP server name, and nothing validates it

`mcp-binding.ts:157-160` calls `toolName` a "Display/audit label only — never sent to the
LLM", and its own wire-shape docstring uses `"tool_name": "My Server"`. But
`agent-defs.ts:916-925` sets `name: b.toolName` as the tool **namespace**, so that example
produces the MCP server name `My Server` and tool ids `mcp__My Server__get_weather`. The two
comments contradict; the second is what runs.

Nothing validates it on either side: not AgentOS (which checks action-name uniqueness across
bindings but not `tool_name`), not the CAS, and not `claude-agent-sdk` — `subprocess_cli.py:660-682`
serialises whatever key it is given straight into `--mcp-config`. A namespace with a space
reaches the CLI unexamined.

The Python builder therefore validates the namespace itself: `[A-Za-z0-9_-]+`, and a binding
that fails it is **skipped with a warning naming the binding and the fix** rather than
attached. Refusing the whole run would be wrong — the other bindings are fine — and attaching
it would produce a server whose tools are unaddressable. Raised as a server-side question in
§9; not fixed there by this work.

### 0.5 Smaller corrections

- **The fork block is gone.** `371c815`'s dead `from .fork import` was removed; `coordinator.py`
  contains no `fork` reference. Phase 3 therefore **introduces the first deep-merge site**,
  rather than sitting after one. The overlay merge it must not break is the shallow
  `merged_options.update(self._options_overlay)` at `coordinator.py:219-221`.
- **The dependency work is already done.** `pyproject.toml` pins `claude-agent-sdk>=0.2.152,<0.3`
  and the lock resolves 0.2.153 with **`mcp` 1.27.2** — not 2.x. Phase 3's floor bump is a
  no-op, and open question 7 is moot for the versions we actually resolve.
- **The fixture is already right.** `tests/fixtures/agent_doc.json` already carries
  `mcp_servers: []` and `mcp_mode: "engine"`. No fixture change needed.
- **`httpx` is not optional.** The plan proposed an `mcp = ["httpx>=0.27"]` extra. `httpx`
  arrives transitively via `claude_agent_sdk → mcp` and cannot be absent — this was established
  when a test asserting otherwise had to be rewritten. **No new extra.**
- **Line numbers refreshed:** the MCP refusal is `agentos_agent.py:183-188`; the test to
  rewrite is `tests/test_agentos_agent_resolve.py:166-172`.
- **`gap.py` creates `allowed_tools`** when a GAP repo declares tools (`gap.py:330-331`), so
  the extend path is live on a GAP source and the absent path is live on a plain repo. Both
  need a test.

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

**Injection into a live run** — `src/computeragent/harness/coordinator.py:244-281` (the fork server). This is the template for MCP injection because it already solved the two hazards:

- The caller options overlay is a shallow `dict.update` and would **clobber** the loader's `mcp_servers`; the fork path deep-merges instead (`:264-266`, and the comment at `:244-249`).
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

**The rule: an `allowed_tools` list is EXTENDED, never CREATED.** `worker-mcp.ts:164-193` states it and this repo already implements it at `coordinator.py:270-271`:

```python
if opts.get("allowed_tools"):
    opts["allowed_tools"] = list(opts["allowed_tools"]) + list(tool_names)
```

Creating a list where none existed flips "every tool allowed" into "only these MCP tools allowed" and silently strips `Read`/`Write`/`Bash`. This was measured, not theorised: on gitagent, a roster entry with `tools: ["write","read"]` produced workers that reported `NO_DEEPWIKI_TOOL`, made **zero** bridge calls, and still came back `completed` under a `pass` scorecard (`worker-mcp.ts:167-174`). Conversely, extending is done for *every* engine, because a model-authored list must be allowed to narrow the built-ins it was given but must not hand back a grant the configuration made (`worker-mcp.ts:174`).

**Namespace collisions.** `tool_name` is a display label that doubles as the namespace (`agent-defs.ts:917-920`); it defaults to `server_id`, which guarantees uniqueness, but an operator can set two bindings to the same `tool_name`. AgentOS validates action-name uniqueness *across* bindings (`mcp-binding.ts:139-152`) but not `tool_name` uniqueness. The Python builder must therefore not silently let one binding's dict key overwrite another's — see Phase 3.

Reserved names to avoid colliding with: `gap_tools` (`gap_tools.py:54`) and `fork` (`coordinator.py:265`).

---

## 7. Phased implementation plan

Each phase is independently shippable and testable. Paths are relative to the repo root.
Rewritten against §0 — where this differs from the original §7, §0 says why.

### Phase 1 — carry the fields, narrow the refusal (no tools attached yet)

New module `src/computeragent/mcp/binding.py`, called from `harness/agentos_agent.py`:

```python
@dataclass(frozen=True)
class McpBinding:
    server_id: str
    action_names: tuple[str, ...]
    tool_name: str          # namespace; defaults to server_id — mcp-binding.ts:157-160
    persist_auth: bool

DEFAULT_MCP_MODE = "engine"   # mcp-binding.ts:44
```

Parse rules ported from `mcp-binding.ts:90-165`: non-list ⇒ error; entry not an object ⇒
error; missing `server_id` ⇒ error; empty `action_names` ⇒ error (`MCP_ACTIONS_REQUIRED`);
the same action name in two bindings ⇒ error (`MCP_DUPLICATE_ACTION`); `mcp_mode` outside
`{engine, studio}` ⇒ error, never a silent fallback. Absent `mcp_servers` and `[]` are both
"no bindings" here — the absent-vs-empty distinction `ParsedMcpBindings` draws matters only to
a PATCH, which this SDK does not perform.

Add `mcp_bindings: tuple[McpBinding, ...]` and `mcp_mode: str` to `ResolvedAgent`. **It still
holds no credential** — that invariant is pinned by an existing test and must survive.

Narrow the blanket refusal at `agentos_agent.py:183-188` into three outcomes, keeping
`UnsupportedAgentError`'s name-the-field contract:

| doc state | outcome |
|---|---|
| no bindings | resolves, exactly as today (regression guard) |
| bindings + `mcp_mode: "studio"` | refuse — Phase 2 |
| bindings + `engine`, no MCP base configured | refuse, naming `mcp_base_url=` / `LYZR_MCP_CLIENT_URL` |
| bindings + `engine` + base configured | resolve, log the server ids |

The third row is the one that matters: never run silently tool-less. Because it depends on
configuration the resolver does not receive, the check lives in `ComputerAgent.__init__`
alongside the `llm_proxy_url` check, which is already the precedent for "the document is fine,
your arguments are not".

**Ships:** better errors. **Tests:** pure parsing + `respx`.

### Phase 2 — `studio` mode refusal

`McpModeUnsupportedError(UnsupportedAgentError)`, message naming the cause and the fix: the
tool loop runs inside Lyzr Studio, so this SDK sees no tool calls — no `execute_tool` spans, no
policy decisions, no HITL — re-register with `mcp_mode: engine`. No `force` flag: a tool-less
run is indistinguishable from a model that chose no tool, which is the ambiguity
`mcp-binding.ts` exists to remove.

### Phase 3 — the in-process Lyzr MCP client

New package `src/computeragent/mcp/`:

| File | Contents |
|---|---|
| `binding.py` | Phase 1 |
| `client.py` | `LyzrMcpClient` over `httpx.AsyncClient`, `x-api-key: <api_key>` (`mcp.ts:352-355`). `list_tools(server_id)` → `GET /api/v1/mcp/servers/{id}/tools`, 60s TTL cache keyed `f"{server_id}::{api_key}"`, **filter after caching** (`mcp.ts:218,243-247`). `execute(server_id, tool_name, arguments)` → `POST /api/v1/mcp/tools/execute` |
| `content.py` | `normalize_content(result) -> list[dict]` — **the §0.1 table**, not a transcription of `normalizeContent`. Docstring cites `mcp.ts:362-428` *and* the two upstream sinks that force the divergence |
| `server.py` | `build_mcp_servers(bindings, client, api_key) -> McpServers` — per binding one `create_sdk_mcp_server(name=tool_name, tools=[...])` plus the `mcp__<tool_name>` grant |

Behaviours carried over, each with its TS citation in a comment:

- **Allowlist enforced at list time and at call time** (`mcp.ts:243-247`, `:266-272`). In-process
  the list is already filtered, but the handler still rejects an out-of-binding name — the model
  can guess, and `create_sdk_mcp_server` would happily dispatch it.
- **Error-channel split** (`mcp.ts:309-334`): `success: false` from the facade is a tool that ran
  and failed ⇒ `{"content": [text], "is_error": True}`, which the model can read and work
  around. A transport failure is *also* `is_error` rather than a raise — an exception out of a
  handler is caught by `run_tool` and turned into `is_error` anyway (`__init__.py:614-615`), so
  raising only loses the message we would have written.
- **Never throw out of boot** (`agent-defs.ts:888-891`): a facade outage degrades the run to
  "no MCP tools this turn". Log at WARN naming the server ids.
- **Zero discovered tools is logged with both lists** — requested action names beside the
  server's real tool names (`mcp.ts:188-192`). This is the most common cause of a bound tool
  never appearing, and the two lists side by side are the whole diagnosis.
- **Schema guard (§0.1 row 3):** a facade `input_schema` is passed through only when it is a
  dict with a string `type` *and* a `properties` key; anything else becomes
  `{"type":"object","properties":{}}`. Otherwise `_build_input_schema` silently reinterprets it
  and every call fails validation.
- **Namespace guard (§0.4):** `tool_name` must match `[A-Za-z0-9_-]+`; a binding that fails is
  skipped with a warning naming it. Reserved names `gap_tools` (`gap_tools.py:54`) and any key
  the loader already produced are skipped the same way.
- **Per-tool closures** (`gap_tools.py:140-161`): one `_build_tool` function per tool, so each
  handler closes over its own spec. The late-binding trap, already paid for once in this repo.

**Wiring** in `HarnessCoordinator._ensure_booted`, after the overlay merge at
`coordinator.py:219-221` and before `harden`. This is the first deep-merge site in the file
(§0.5), so it carries the comment explaining why a shallow `update` is wrong here:

```python
mcp = dict(merged_options.get("mcp_servers") or {})   # gap_tools lives here
for name, cfg in built.servers.items():
    if name in mcp:
        logger.warning(...)   # never silently overwrite a loader-supplied server
        continue
    mcp[name] = cfg
merged_options["mcp_servers"] = mcp
if merged_options.get("allowed_tools"):               # EXTEND ONLY — worker-mcp.ts:164-193
    merged_options["allowed_tools"] = [*merged_options["allowed_tools"], *built.grants]
```

Attached for `claude-agent-sdk` only; any other engine gets a warning and no servers, matching
`applyMcpServers`'s refusal to write an Anthropic-shaped allowlist for an engine that cannot
read it (`index.ts:4237-4256`).

`mcp_base_url=` is added to `ComputerAgent.__init__` next to `llm_proxy_url`, defaulting to
`os.environ.get("LYZR_MCP_CLIENT_URL")` (§0.2). No new extra, no dependency change (§0.5).

### Phase 4 — governance verification (not optional)

§0.3: `allowed_tools` is inert under this run mode's default permission mode, so this phase is
where the feature's safety claim is actually established.

- An MCP tool call produces an `execute_tool` span with `gen_ai.tool.name == "mcp__<ns>__<tool>"`
  (`telemetry/sinks/otel.py:1053-1066`).
- A `policy_decider` denying `mcp__*` blocks the call under `permission_mode="default"` (via
  `can_use_tool`) **and** under `bypassPermissions` (via the `PreToolUse` hook,
  `engines/claude_agent.py:154-172`). The second is the one that matters here.
- The guardrails middleware's `mcp__*` glob (`telemetry/middleware/guardrails.py:115-118`)
  matches the generated names.
- `on_tool_call` — note it is *already* warned as inert on this engine
  (`harness/__init__.py`), so the assertion is that the warning is honest, not that the
  callback fires.

### Phase 5 — the HTTP-bridge escape hatch

`mcp_bridge_url=`: when set, skip the in-process builder and emit CAS-identical entries —

```python
{"type": "http",
 "url": f"{base}/mcp/{quote(b.server_id)}?tools={quote(','.join(b.action_names))}",
 "headers": {"x-api-key": api_key},
 "alwaysLoad": True}
```

byte-for-byte `index.ts:4270-4282`, grants `mcp__<tool_name>` (`:4285`). Worth keeping because
it is one kwarg and zero maintenance, it is right for a deployment already fronted by the CAS
proxy, and it is the escape hatch if the facade changes and the Python port lags. Document that
the bridge 401s without the header and that the URL must be the proxy's externally reachable
origin, not CAS loopback.

Direct HTTP servers from a GAP repo (`gap.py:315-341`) already work and are untouched — the
Phase 3 merge skips existing keys.

### Phase 6 — documentation

`docs/USING_THE_SDK.md` gains an MCP section (inputs, the `engine`-only rule, why `studio` is
refused, and the extend-never-create note for anyone passing `options={"allowed_tools": [...]}`);
`docs/ARCHITECTURE.md` gains the two paths; `CHANGELOG.md` + a version bump. The published
artifact is re-published from the same source.

---

## 8. Test plan

Conventions as the repo already uses them: `pytest` with `asyncio_mode = "auto"` and
`--strict-markers`, `respx` for HTTP, the `integration` marker for anything live, and the
in-process-MCP assertion style of `tests/test_gap_tools.py:35-54`.

`tests/fixtures/agent_doc.json` already carries `mcp_servers: []` / `mcp_mode: "engine"` (§0.5).
One new fixture: `stub_lyzr_mcp`, an in-memory `LyzrMcpClient` double recording calls and
returning scripted payloads, so `server.py` needs no network.

| File | Covers |
|---|---|
| `test_mcp_binding.py` | parity with `mcp-binding.ts:90-165` — non-list, non-object entry, missing `server_id`, empty `action_names`, duplicate action across bindings, `tool_name` defaults to `server_id`, `persist_auth` strictly `is True`, unknown `mcp_mode` rejected, absent ⇒ `"engine"` |
| `test_mcp_client.py` (`respx`) | `input_schema` carried through; allowlist filtering; cache serves a second call with `route.call_count == 1`; two credentials do **not** share an entry; non-2xx raises typed; `execute` sends `{server_id, tool_name, arguments}` + `x-api-key` |
| `test_mcp_content.py` | the **§0.1 table**: `None` ⇒ `[]`; bare string ⇒ one text block; each of text/image/resource_link/resource passes through; **`structured` ⇒ a text block, not hoisted**; **`audio` ⇒ a text block, not passed through**; unknown type ⇒ text; non-object ⇒ `str()`. The two bold rows carry a comment citing the upstream line that forces them |
| `test_mcp_server_build.py` | one binding ⇒ one `type: "sdk"` server keyed by `tool_name`, grants `["mcp__<tool_name>"]`, per-action tools; a facade failure at boot ⇒ zero servers, no exception; `success: False` ⇒ `is_error` with the upstream message; a transport exception ⇒ `is_error`, not a raise; an out-of-allowlist name is refused by the handler; a `tool_name` with a space is skipped with a warning (§0.4); a malformed `input_schema` is normalised (§0.1 row 3); **each handler closes over its own tool** |
| `test_mcp_coordinator_wiring.py` | servers land under `mcp_servers` **alongside** `gap_tools` without clobbering it; a pre-existing key of the same name is skipped with a warning; an existing `allowed_tools` is extended; **an absent `allowed_tools` stays absent**; nothing attaches for a non-`claude-agent-sdk` engine; the caller's `options={"mcp_servers": …}` overlay is not destroyed by the merge |
| `test_agentos_agent_resolve.py` (extend) | replaces `:166-172` with the Phase 1 four-row table; `ResolvedAgent` repr still carries no credential |
| `test_mcp_policy.py` | Phase 4 — deny on `mcp__*` under **both** `default` and `bypassPermissions`; an `execute_tool` span is emitted; the guardrails glob matches |
| `test_mcp_integration.py` | `@pytest.mark.integration`, skipped without `LYZR_MCP_CLIENT_URL` + a key. The only place the real facade is touched — and the only place open question 3 gets answered |

Gates as before: `pytest -q -m "not integration"`, `ruff check`, `ruff format --check`.

---

## 9. Open questions

Reduced from nine — §0 closed four (2, 6, 7, and the `371c815` half of 1) and reframed two.

1. **Does the workspace `api_key` authenticate against `/api/v1/mcp/*`?** **Probed live on
   2026-09-16 against `https://mcp-dev.test.studio.lyzr.ai`, and still open** — because the list
   route turns out not to be an auth oracle at all:

   ```
   no x-api-key header   → 422 {"detail":[{"type":"missing", …}]}    (FastAPI validation)
   x-api-key: garbage    → 200 {"servers": [], "total": 0}
   x-api-key: real key   → 200 {"servers": [], "total": 0}
   ```

   A bogus key and a real one are indistinguishable on an account with no servers registered, so a
   200 establishes nothing. Two consequences, both now pinned by tests in
   `tests/test_mcp_integration.py`: **code branching on 401 to mean "bad MCP credential" is wrong
   for this service**, and the question can only be settled against an account that actually has a
   server registered, or through the execute route. `client.py` remains correct by construction —
   it sends exactly what the hosted bridge sends — but unconfirmed by observation.

2. **`persist_auth` appears inert under `engine` mode.** Parsed, stored, echoed and warned about
   for group-owned agents, but read by nothing in `mcpDescriptorsFor`, `applyMcpServers` or
   `mcp.ts` — `McpDescriptor` has no such field. Presumably meaningful only inside Lyzr's own
   service. Parsed and carried; nothing acts on it.
3. **Pagination and rate limits on the facade.** `mcp.ts:193-195` asserts the whole tool list
   comes back with no cursor, but that is an assertion about today's service, not a contract.
   Unknown for `tools/execute`.
4. **Duplicate `tool_name` across bindings, and `tool_name` validity.** AgentOS enforces
   action-name uniqueness but not `tool_name` uniqueness or shape (§0.4), and the CAS would let a
   duplicate silently overwrite a key. Python skips-and-warns for both. **Whether AgentOS should
   reject a non-identifier `tool_name` at bind time is a server-side question raised here, not
   answered** — and its own docstring example, `"My Server"`, would fail that check.
5. **gitagent.** `GitAgentEngine` is MCP-stub, so the `<server>__<tool>` grant shape from
   `worker-mcp.ts:86` has no Python consumer. Documented in §6; not implemented.
