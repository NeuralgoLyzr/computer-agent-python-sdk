# MCP support — end-to-end test plan

Companion to `MCP_SUPPORT_PLAN.md`. That document says what to build; this one says how we
know it works, at every layer from a pure function to a live model calling a real tool.

**Status:** plan only. Written against the repo at `feat/mcp-support`, `claude-agent-sdk`
0.2.153 / `mcp` 1.27.2.

---

## 0. What this has to prove, and why the usual shape is not enough

Three properties, in descending order of how badly a miss would hurt:

1. **A bound tool is callable and its result reaches the model intact.** The failure mode is
   not a crash. It is a tool that appears in the list, gets called, and returns something the
   model reads as an error — which is what the TS bridge's `normalizeContent` exists to
   prevent, and which §0.1 of the support plan shows the Python path can reproduce *in two new
   ways of its own*.
2. **Every MCP call is governed.** `engine` mode exists so tool calls pass through policy and
   produce spans (`mcp-binding.ts:32-43`). If that is not true, the feature is worse than not
   shipping it: runs look clean and are ungoverned.
3. **A misconfiguration fails loudly.** Every rule in `mcp-binding.ts` exists because Studio
   accepts a broken binding silently. The Python port inherits that obligation.

Property 1 is why this plan has a live layer at all. Properties 2 and 3 are provable offline;
property 1 has a component — *what the facade actually returns, and whether our key reaches
it* — that no double can establish.

**The asymmetry that shapes everything below:** the Lyzr MCP service is a remote,
credentialed, multi-tenant service, and (per `MCP_PHASE2_PLAN.md:252-253`) it cannot reach a
locally-hosted MCP server — so we cannot stand up a tool for it to proxy. We can only call
tools that already exist in someone's account. That makes the real facade unsuitable as the
*routine* e2e substrate and necessary as the *contract* check. Hence two live modes (§5).

---

## 1. Layers

| # | Layer | Substrate | Proves | Runs |
|---|---|---|---|---|
| 0 | Fixtures & doubles | — | the doubles are faithful | — |
| 1 | Unit | pure / `respx` | parsing, translation, client | every commit |
| 2 | Wiring | fake engine, stub client | the tools reach `ClaudeAgentOptions` intact | every commit |
| 3 | Governance | `upstream_query_factory` | spans, policy, guardrails | every commit |
| 4 | Contract | **fake facade**, real CLI, real model | the whole path, laptop-runnable | on demand |
| 5 | Live | **real facade**, real CLI, real model | our key works; the facade is as documented | gated |

Layers 1–3 are `pytest` and must stay green in CI. Layer 4 is a script. Layer 5 is a script
flag plus one `integration`-marked test.

---

## 2. Layer 0 — fixtures and doubles

### 2.1 Reused as-is

From `tests/conftest.py`: `_reset_pipeline` (autouse, so telemetry never leaks between tests),
`StubSink` / `stub_sink`, `upstream_query_factory` and `fake_upstream_client` (both
monkeypatch `claude_agent_sdk.query`), and `agent_doc` — which **already carries
`mcp_servers: []` and `mcp_mode: "engine"`**, so no fixture change is needed.

From `tests/test_gap_tools.py:35-54`: the in-process-MCP assertion style — assert on
`result.mcp_server["type"] == "sdk"` and `["name"]`, and on the qualified `mcp__…` names.

### 2.2 New — `stub_lyzr_mcp`

An in-memory `LyzrMcpClient` double. Records every `list_tools` / `execute` call (server id,
tool name, arguments) and returns scripted payloads, including scripted *failures*: a raise, a
`{"success": false, "error": …}`, an empty tool list.

**Faithfulness is the risk with any double**, so it is pinned by construction: `stub_lyzr_mcp`
and the real client are both exercised against the **same** payload corpus (§2.3), the stub by
returning them and the real client by `respx`-serving them. A behaviour that only the stub has
cannot survive that.

### 2.3 `tests/fixtures/mcp_payloads.json` — one corpus, three consumers

Every facade response shape we know of, in one file, consumed by the content tests, the client
tests and the stub. Seeded from the shapes `mcp.ts` calls out by name:

| key | `result` | source |
|---|---|---|
| `text_only` | `[{"type":"text","text":"…"}]` | ordinary |
| `structured_pair` | `[{"type":"text",…},{"type":"structured","data":{…}}]` | the observed `read_wiki_structure` response, `mcp.ts:369-372` |
| `bare_string` | `"just a string"` | `mcp.ts:393` |
| `null_result` | `null` | `mcp.ts:392` |
| `every_mcp_type` | one block each of text/image/audio/resource_link/resource | `MCP_CONTENT_TYPES`, `mcp.ts:363` |
| `unknown_type` | `[{"type":"diagram","svg":"…"}]` | `mcp.ts:411-412` |
| `non_object` | `[42, null, "x"]` | `mcp.ts:403-406` |
| `tool_failed` | `{"success": false, "error": "upstream 500"}` | `mcp.ts:318-334` |

Adding a row when the facade surprises us is how this file earns its keep — a new non-MCP
block type is exactly how the TS broke the first time, and it was invisible until a model
reported a failure.

### 2.4 The fake facade — `tests/fakes/lyzr_mcp_facade.py`

A ~60-line `http.server` (or `respx`-independent `uvicorn`-free) app serving the two real
routes over a real socket:

```
GET  /api/v1/mcp/servers/{id}/tools   → {"tools": [{name, description, input_schema}]}
POST /api/v1/mcp/tools/execute        → a row from the corpus, chosen by tool_name
401 on any request without x-api-key
```

It is the same corpus, served over HTTP. It exists because layers 4 and the real CLI need a
*socket*, not a Python object — and because it is the only way to run the complete path
(model → CLI → in-process MCP server → our client → HTTP → response → model) without the
remote service. Used by layer 4 and by one `integration` test.

---

## 3. Layer 1 — unit

### 3.1 `test_mcp_binding.py` — parity with `mcp-binding.ts:90-165`

Pure, no I/O. Each case cites the TS rule it mirrors.

| Case | Input | Expect |
|---|---|---|
| absent | no `mcp_servers` key | `()` , no error |
| explicit empty | `[]` | `()` , no error |
| non-list | `"srv_1"` | error, message names `mcp_servers` |
| non-object entry | `["srv_1"]` | error |
| missing server_id | `[{"action_names":["x"]}]` | error naming `server_id` |
| blank server_id | `[{"server_id":"  ", …}]` | error — TS trims then checks |
| actions not a list | `action_names: "x"` | error |
| actions not all strings | `["x", 3]` | error |
| **empty actions** | `action_names: []` | `MCP_ACTIONS_REQUIRED` — "an empty list silently exposes no tools" |
| actions blank-only | `["", "  "]` | `MCP_ACTIONS_REQUIRED` (trim-then-filter leaves none) |
| **duplicate action** | two bindings both claiming `search` | `MCP_DUPLICATE_ACTION`, message names **both** server ids |
| tool_name default | no `tool_name` | `tool_name == server_id` |
| tool_name blank | `"   "` | falls back to `server_id` |
| persist_auth | `"true"` (string) | `False` — TS is `=== true` |
| mode absent | no `mcp_mode` | `"engine"` |
| mode `"studio"` | | `"studio"` |
| **mode `"Engine"`** | | error, not a silent fallback — `mcp-binding.ts:55-67` |

### 3.2 `test_mcp_content.py` — the divergence file

The most important unit file, because this is where "port it verbatim" is wrong. Each row
asserts a **Python-correct** output; the two divergent rows carry a comment naming the upstream
sink that forces them.

| Input | Expect | Note |
|---|---|---|
| `None` / absent | `[]` | same as TS |
| `"a string"` | `[{"type":"text","text":"a string"}]` | same as TS |
| `text` block | passed through | same |
| `image` block | passed through | same |
| `resource_link` | passed through | same — upstream flattens it to text itself |
| `resource` (text) | passed through | same |
| **`audio`** | **`{"type":"text", …}` JSON-stringified** | **DIVERGES.** `mcp.ts:363` passes it; `_convert_tool_content` drops it with a warning |
| **`{"type":"structured","data":…}`** | **a text block in `content`** | **DIVERGES.** `mcp.ts:413` hoists it to `structuredContent`; `run_tool` builds `CallToolResult` from `content`+`isError` only, so hoisting deletes it |
| second `structured` | **also a text block** — both survive | TS keeps the first and drops the second; we have no reason to lose either |
| unknown type | JSON text block | same |
| non-object element | `str()` text block | same |
| a `dict` (not a list) | treated as one block | `mcp.ts:395` |
| mixed | order preserved | |

Plus: **the coercion log fires once per call, not once per block** (`mcp.ts:399-401`,
`:424-429`), listing the distinct coerced types. Asserted with `caplog`.

**Guard test — `test_upstream_content_union_unchanged`.** The two divergences are true of
`claude-agent-sdk` 0.2.153 and could be fixed upstream. A test introspects
`claude_agent_sdk._convert_tool_content` against one block of each type and **fails if `audio`
or `structuredContent` ever starts surviving** — at which point this file's expectations are
the thing to revisit, deliberately, rather than discovering it as drift years later.

### 3.3 `test_mcp_client.py` — `respx`

| Case | Assert |
|---|---|
| list_tools happy | `GET /api/v1/mcp/servers/{id}/tools`, id URL-encoded; `input_schema` carried through |
| allowlist filter | only bound names returned |
| empty allowlist | everything returned (`mcp.ts:244`) |
| **cache hit** | second call ⇒ `route.call_count == 1` |
| **cache expiry** | past TTL ⇒ `== 2` (injected clock, no sleeping) |
| **per-credential cache** | two keys, same server ⇒ two requests; neither list leaks to the other |
| **filter after caching** | narrow binding then wide binding, same server ⇒ one request, wide gets everything (`mcp.ts:243-247`) |
| schema absent | `{"type":"object","properties":{}}` (`mcp.ts:238`) |
| **schema malformed** | `{"type":"object"}` (no `properties`) ⇒ normalised — §0.1 row 3 |
| execute shape | body is exactly `{server_id, tool_name, arguments}` |
| header | `x-api-key` present; **no `authorization`** |
| non-2xx | typed error carrying status + a truncated body |
| **secret hygiene** | the api_key appears in no exception message and no `repr()` — parametrized over every failure path |

### 3.4 `test_mcp_server_build.py` — stub client

| Case | Assert |
|---|---|
| one binding | one `type:"sdk"` server keyed by `tool_name`; grants `["mcp__<tool_name>"]`; one tool per action |
| tool naming | the CLI-visible name is `mcp__<tool_name>__<action>` |
| **per-tool closures** | build two tools, call the first, assert the *second's* name was never sent — the `gap_tools.py:140-161` late-binding trap, already paid for once here |
| `success: false` | `{"is_error": True}` carrying the upstream message |
| transport raise | `is_error`, **not** a propagated exception |
| **boot failure** | `list_tools` raises ⇒ zero servers, no exception, WARN naming the server ids (`agent-defs.ts:888-891`) |
| **zero tools discovered** | WARN contains *both* the requested action names and the server's real tool names (`mcp.ts:188-192`) |
| call-time allowlist | a handler invoked with an unbound name refuses without calling `execute` (`mcp.ts:266-272`) |
| **namespace with a space** | `tool_name="My Server"` ⇒ binding skipped, WARN names it — §0.4 |
| namespace reserved | `tool_name="gap_tools"` ⇒ skipped |
| **duplicate tool_name** | two bindings, same name ⇒ first wins, second skipped with a WARN (never a silent overwrite, unlike `index.ts:4272`) |

---

## 4. Layers 2 and 3 — wiring and governance

### 4.1 `test_mcp_coordinator_wiring.py`

Build a coordinator, run `_ensure_booted`, assert on `coord._manifest.options`. The merge site
is `coordinator.py:219-221`, today a shallow `update` and the first deep-merge site in the file.

- MCP servers land under `mcp_servers` **alongside** `gap_tools` — neither clobbers the other.
- A caller's `options={"mcp_servers": {…}}` survives the merge. *(This is the regression the
  shallow `update` would cause, and the reason the fork path deep-merged.)*
- A pre-existing key of the same name is skipped with a WARN.
- An existing `allowed_tools` is **extended**, order preserved, no duplicates introduced.
- **An absent `allowed_tools` stays absent.** Creating one flips "every tool allowed" into
  "only these", silently stripping `Read`/`Write`/`Bash` (`worker-mcp.ts:167-174`, where it was
  measured, not theorised). Both branches are live in this repo: `gap.py:330-331` creates the
  list on a GAP source, and a plain repo has none.
- Nothing attaches for a non-`claude-agent-sdk` engine; a WARN says so.
- Nothing attaches when construction already refused (`studio` mode).
- `harden` still runs after the MCP merge, so a GAP repo's compliance hardening is not bypassed.

### 4.2 `test_agentos_agent_resolve.py` — extend in place

Replaces the single `test_refuses_bound_mcp_servers` (`:166-172`) with Phase 1's four rows: no
bindings resolves as today (regression guard for the relaxed refusal); `studio` raises
`McpModeUnsupportedError`; `engine` + no base configured raises naming `mcp_base_url=` /
`LYZR_MCP_CLIENT_URL`; `engine` + configured populates `ResolvedAgent.mcp_bindings`. Plus the
existing invariant: **`repr(ResolvedAgent)` still contains no credential.**

### 4.3 `test_mcp_policy.py` — property 2

Uses `upstream_query_factory` to script a turn containing an MCP `tool_use` block.

- An `execute_tool` span is emitted with `gen_ai.tool.name == "mcp__<ns>__<tool>"`
  (`telemetry/sinks/otel.py:1053-1066`).
- A `policy_decider` denying `mcp__*` blocks the call under `permission_mode="default"` (via
  `can_use_tool`) **and** under `bypassPermissions` (via the `PreToolUse` hook,
  `engines/claude_agent.py:154-172`). **The second assertion is the load-bearing one** —
  `bypassPermissions` is this run mode's default, and it is the mode in which `allowed_tools`
  grants and forbids nothing (§0.3).
- The guardrails `mcp__*` glob (`telemetry/middleware/guardrails.py:115-118`) matches the
  generated names.
- A denial is visible in telemetry as a `policy_decision`, correlated to the session.

---

## 5. Layer 4 and 5 — live

`examples/e2e/mcp_live_run.py`, modelled on `live_agent_run.py`'s staged-checkpoint shape
(`stage()` / `ok()` / `bad()` / `info()`, argparse with env fallbacks). Staged so a failure
**localizes** instead of presenting as "the agent didn't use the tool" — which is the single
least diagnosable outcome this feature has.

Two modes, one script:

| Mode | Flag | Facade | Answers |
|---|---|---|---|
| **Contract** | `--fake-facade` | `tests/fakes/lyzr_mcp_facade.py` on a free port | does the whole path work? Runnable on a laptop, deterministic, no credentials |
| **Live** | `--mcp-base-url` | the real service | does *our key* work, and is the facade as documented? |

### Stages

| # | Stage | Pass | Failure tells you |
|---|---|---|---|
| 1 | Resolve the agent | `mcp_bindings` non-empty, `mcp_mode == "engine"` | the agent has no bindings — fix it in AgentOS, not here |
| 2 | **Preflight the facade** | `GET /servers/{id}/tools` returns ≥1 tool | base unreachable / key rejected / server id unknown — **three distinct messages**, because from inside the SDK all three later look like "no tools" |
| 3 | Preflight the model path | proxy `/health` and gateway reachable | reuses `live_agent_run.py`'s stage 2 verbatim — these gateways are tunnels whose hostnames go stale |
| 4 | Build the servers | one `sdk` server per binding, expected grants, expected tool names | a binding was skipped — the WARN names which and why |
| 5 | Options reach the engine | `ClaudeAgentOptions.mcp_servers` carries them; `allowed_tools` extended-or-absent per §4.1 | the merge clobbered something |
| 6 | **The model calls the tool** | a `tool_use` block named `mcp__<ns>__<tool>` appears | the model saw the tool and chose not to use it (prompt), or never saw it (stage 4 lied) |
| 7 | **The result is well-formed** | the following `tool_result` is **not** `is_error`, and its text contains the corpus sentinel | this is property 1. An `is_error` here with a successful upstream call is the exact `invalid_union` failure `normalizeContent` exists to prevent |
| 8 | Governance | an `execute_tool` span exists for the call; with `--deny-mcp`, the call is blocked and a `policy_decision` is recorded | the feature ran ungoverned |
| 9 | Second turn | a resumed turn still has the tools | `alwaysLoad` / session-resume interaction (open question 6) |

Stage 7 uses a **sentinel string** planted in the fake facade's response, so "the tool
returned" and "the model paraphrased something plausible" cannot be confused — the single
easiest way to fool yourself in an agent e2e.

### `test_mcp_integration.py`

`@pytest.mark.integration`, skipped unless `LYZR_MCP_CLIENT_URL` and a workspace key are
present. Lists the real tools of a known `srv_…`, executes one, asserts a well-formed result.

**This is where open question 1 gets answered** — whether a workspace `api_key` authenticates
against `/api/v1/mcp/*`, given that AgentOS reaches only the *read* half, on a different host,
with a Studio key. Until this passes, Phase 3 is not done. A 401 here is a finding about the
deployment, not a bug in the SDK, and the test's failure message must say so.

---

## 6. Parity with the TypeScript bridge

`packages/llm-proxy-openai/src/mcp.test.ts` (361 lines) is the behavioural spec. Its cases map
onto this plan as follows — and the mapping is the artifact worth keeping, because the gaps are
deliberate:

| TS case | Python home | Note |
|---|---|---|
| handshake / ping / notification 202 / unknown method / 404 when disabled | **none** | JSON-RPC half; in-process there is no listener. ~110 lines of `mcp.ts` do not port |
| `input_schema` → `inputSchema` | `test_mcp_client.py` | |
| allowlist filter, list + call | `test_mcp_client.py`, `test_mcp_server_build.py` | |
| cache TTL / per-credential / filter-after-cache | `test_mcp_client.py` | |
| `success:false` ⇒ `isError`, not JSON-RPC error | `test_mcp_server_build.py` | |
| transport failure ⇒ JSON-RPC error | `test_mcp_server_build.py` | **diverges**: in-process there is no error channel, so it is `is_error` too |
| **"hoists `structured` into structuredContent"** | `test_mcp_content.py` | **inverted** — we keep it in `content`. §0.1 row 1 |
| **"passes every valid MCP block type through untouched"** | `test_mcp_content.py` | **narrowed** — `audio` becomes text. §0.1 row 2 |
| stringifies unknown / normalizes bare string / omits absent structuredContent | `test_mcp_content.py` | |
| forwards `x-api-key`; accepts Bearer; 401 without one | `test_mcp_client.py` (send side only) | we are a client, never a server — the 401 cases have no counterpart, which is the security property of choosing in-process (§5 of the support plan) |

Four TS behaviours have **no** Python counterpart, all for the same structural reason: there is
nothing listening. That is the point, and this table is where it is recorded rather than
looking like missing coverage.

---

## 7. Failure catalogue

What to check first, keyed by what you actually see. This is the deliverable an operator uses.

| Symptom | Most likely | Where |
|---|---|---|
| Agent never mentions a tool | binding skipped at build | stage 4 WARN; `test_mcp_server_build.py` namespace/reserved cases |
| `tools/list` empty | action names don't match the server's real tool names | stage 2 prints both lists side by side |
| Every call `is_error` immediately, no HTTP | jsonschema rejected the args before the handler | §0.1 row 3/4 — check the facade's `input_schema` |
| Call succeeds upstream, model sees an error | content coercion | `test_mcp_content.py`; historically `invalid_union` |
| Tool data silently missing from the reply | `audio` / `structured` dropped | the two divergence rows; `test_upstream_content_union_unchanged` |
| Tools vanish on turn 2 | resume / `alwaysLoad` | stage 9 |
| `Read`/`Bash` stop working after adding MCP | `allowed_tools` was **created** | `test_mcp_coordinator_wiring.py` absent-stays-absent |
| 401 from the facade | the workspace key is not accepted there | open question 1 — `test_mcp_integration.py` |
| Works locally, not in CI | the real facade cannot reach a local MCP server | use `--fake-facade`; the real service is remote by design |

---

## 8. Gates and sequencing

```
uv run pytest -q -m "not integration"      # layers 1-3, every commit
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q -m integration            # layer 5, gated on env
uv run python examples/e2e/mcp_live_run.py --fake-facade …   # layer 4
```

Tests land **with** their phase, not after it:

| Phase | Tests that must be green to call it done |
|---|---|
| 1 — carry the fields | `test_mcp_binding.py`, `test_agentos_agent_resolve.py` |
| 2 — studio refusal | the `studio` row |
| 3 — the client | `test_mcp_content.py`, `test_mcp_client.py`, `test_mcp_server_build.py`, `test_mcp_coordinator_wiring.py`, **and** layer 4 with `--fake-facade` |
| 4 — governance | `test_mcp_policy.py`, stage 8 |
| 5 — bridge escape hatch | a byte-for-byte URL/header test against `index.ts:4270-4282` |

Phase 3 is **not** done on unit tests alone. Its two hardest properties — the content path and
whether our credential works — are exactly the two that only layers 4 and 5 establish.
