# Using the SDK

`computer-agent-py` runs an agent that is **registered in AgentOS** on **your machine, in your process**.

You name the agent by id. The SDK fetches its definition from AgentOS and reproduces what the AgentOS
dispatch path would have sent to a ComputerAgent server — same source, same model, same engine
environment, same permission mode — except the engine runs in this process instead of in a sandbox.

That is the only run mode. There is no remote harness, no sandbox client, and no way to construct an
agent without AgentOS resolving it first.

Everything beyond the three required arguments is an override for something the agent document already
answers.

---

## 1. Install

Requires **Python ≥ 3.10**.

```bash
pip install 'computer-agent-py[agentos]'
# or, in this repo
uv sync --all-extras --dev
```

`[agentos]` is not optional in practice: it pulls `httpx`, and resolving an agent is an HTTP call. Without it
construction fails with *"resolving an agent requires httpx — install 'computer-agent-py[agentos]'"*.

| Extra | Pulls | Needed for |
|---|---|---|
| *(base)* | `claude-agent-sdk`, `typing-extensions` | the `claude-agent-sdk` engine |
| `agentos` | `httpx` | **resolving the agent** (required), the AgentOS telemetry sink, the git-credential route |
| `otel` | `opentelemetry-*` | the OpenTelemetry telemetry sink |
| `gitagent` | `openai`, `anthropic`, `pyyaml` | the `gitagent` engine |
| `gap` | `pyyaml` | reading a GAP repo's `agent.yaml` (most registered agents are GAP repos) |
| `mongo` | `pymongo` | the `mongo` session store *(this area is in flight — see §7)* |
| `sqlite` | `aiosqlite` | — |
| `cedar` | `cedarpy` | `CedarPolicyEngine` |
| `all` | all of the above | everything |

**Also required for the `claude-agent-sdk` engine:** the Claude Code CLI on `PATH`, since the engine spawns
it as a subprocess.

```bash
npm install -g @anthropic-ai/claude-code
```

**Also required for a cloned source:** `git` on `PATH`.

---

## 2. Quickstart

The smallest thing that runs:

```python
import asyncio
from computeragent import ComputerAgent

async def main():
    async with ComputerAgent(
        base_url="http://localhost:8788/agentos/api",
        agent_id="6aa93f1b3817a13d3103d2c1",
        api_key="<lyzr workspace key>",
        llm_proxy_url="http://127.0.0.1:8786",   # see §6 — needed for a gateway-bound agent
    ) as agent:
        result = await agent.chat("List the files in your working directory.")
        print(result.messages[-1])
        print(result.usage)

asyncio.run(main())
```

The same with the options people actually reach for:

```python
from pathlib import Path

async with ComputerAgent(
    base_url="http://localhost:8788/agentos/api",
    agent_id="6aa93f1b3817a13d3103d2c1",
    api_key=os.environ["LYZR_API_KEY"],
    llm_proxy_url="http://127.0.0.1:8786",
    source=Path("/path/to/local/checkout"),      # skip the clone, run against this directory
    options={
        "system_prompt": "You are terse.",
        "allowed_tools": ["Read", "Glob"],
        "max_turns": 8,
    },
    session_store="memory",
    budget_max_usd=0.50,
) as agent:
    handle = agent.chat("Summarise the repo layout.")
    async for event in handle:
        print(event.kind)
    result = await handle
```

`ComputerAgent(...)` does the AgentOS resolve **synchronously in the constructor**, so a bad id, a bad key or
an unsupported agent shape raises before anything boots. The workspace is materialised lazily on the first
`chat()`.

---

## 3. Where each value comes from

| Value | Where you get it |
|---|---|
| `agent_id` | The agent's **24-hex Mongo `_id`**. In the AgentOS SPA, open the agent — it is the id in the URL / the agent's detail header. Equivalently `GET {base}/v1/agents` and take `id`. **Not** the stable `agentId` field: the route rejects a non-ObjectId outright with a 404. |
| `base_url` | The AgentOS deployment's API base, e.g. `http://localhost:8788/agentos/api` for the local docker stack, or your deployed AgentOS host. Three spellings accepted — see below. |
| `api_key` | Your **Lyzr workspace key**, from the Lyzr Studio / AgentOS workspace settings ("API keys"). It travels as the `x-api-key` header. |
| `llm_proxy_url` | The base URL of the Anthropic-to-OpenAI translating proxy you (or your platform team) run, e.g. `http://127.0.0.1:8786`. It has a `/health` endpoint you can curl. Required only in the case in §6. |
| `git_token` | A git host PAT with read access to the agent's source repo (GitHub: Settings → Developer settings → personal access tokens; `repo`/`contents:read` scope). Only for a **private** source. |

### `base_url` — three accepted spellings

`api_base()` normalises whatever you pass to the `/v1` API root, so a trailing slash or an already-versioned
base never doubles the path:

| You pass | Resolves to |
|---|---|
| `http://host:8788` | `http://host:8788/agentos/api/v1` |
| `http://host:8788/agentos/api` | `http://host:8788/agentos/api/v1` |
| `http://host:8788/agentos/api/v1` | unchanged |

Trailing slashes are stripped first, so `.../agentos/api/` behaves like `.../agentos/api`. An empty or
`/`-only `base_url` raises `AgentResolveError("base_url is empty …")`.

The resolve call the SDK then makes is:

```
GET {base_url normalised}/agents/{agent_id}      header: x-api-key: <lyzr key>
```

---

## 4. Constructor reference

All parameters are **keyword-only**.

| Parameter | Type | Required | Default | Notes |
|---|---|---|---|---|
| `agent_id` | `str` | **yes** | — | 24-hex Mongo `_id`. |
| `base_url` | `str` | **yes** | — | AgentOS API base; three spellings (§3). |
| `api_key` | `str` | **yes** | — | Lyzr workspace key, sent as `x-api-key`. |
| `llm_proxy_url` | `str \| None` | conditional | `None` | Required for an Anthropic-shaped engine against a native gateway (§6); unused otherwise. |
| `git_token` | `str \| None` | conditional | `None` | Only for a **private** source. Without one the SDK tries AgentOS's GitHub-App credential route, then clones anonymously. |
| `source` | `Path \| str \| dict \| None` | no | agent document's `source` | Workspace override — see below. |
| `engine` | `str \| EngineDriver \| None` | no | agent document's `harness` | `"claude-agent-sdk"` or `"gitagent"`, or your own `EngineDriver` instance. An explicit value also selects which gateway-env branch is built. |
| `options` | `Mapping[str, Any] \| None` | no | `None` | Engine options; merged over what the agent resolves, caller wins (§5). |
| `envs` | `Mapping[str, str] \| None` | no | `None` | Extra environment for the engine subprocess. Gateway envs are layered **over** yours. |
| `session_store` | `str \| SessionStore \| Mapping \| None` | no | `None` (no store) | `"memory"`, a descriptor `{"kind": ..., "options": {...}}`, or a store instance (§7). |
| `session_id` | `str \| None` | no | generated `ca_…` | Pins the **telemetry** lifecycle session id. Does not pin the CLI's own session id — for that set `options={"session_id": ...}`. |
| `project_key` | `str` | no | `"default"` | Accepted and carried, but the harness does not currently thread it into the session-store key — the Claude SDK derives its own project key from the workspace cwd. |
| `agent_name` | `str \| None` | no | agent document's `name` | Telemetry label only. |
| `agent_description` | `str \| None` | no | `None` | Telemetry label only. |
| `policy_decider` | `PolicyDecider \| None` | no | `None` | Async `(tool_name, tool_input, context) -> decision`. E.g. `computeragent.PolicyToolAuthorizer` (§9). |
| `on_tool_call` | `Callable[[PermissionRequest], Awaitable[dict]] \| None` | no | `None` | Per-tool gate callback. Consulted **after** `policy_decider`; default when neither is set is allow. |
| `budget_max_usd` | `float \| None` | no | `None` | Carried into `EngineContext`. **No built-in engine enforces it today** — treat it as advisory metadata, or set `options={"max_budget_usd": ...}` which the Claude CLI does honour. |
| `telemetry_pipeline` | `Pipeline \| None` | no | the module-level pipeline | Override the telemetry pipeline for this agent (§9). |

### `source` — how a value is interpreted

| You pass | What happens |
|---|---|
| `Path("/some/dir")` | Used in place as the workspace. Caller-owned: never copied, never deleted. |
| `str` starting `git@`, `ssh://`, `git://` | `git clone`d into a temp dir, removed on dispose. |
| `str` starting `https://`/`http://` **and** ending `.git`, **or** containing a known git host (`github.com`, `gitlab.com`, `bitbucket.org`, `git.sr.ht`, `codeberg.org`) | Cloned as above. |
| any other `str` | Treated as a filesystem path and coerced to `Path`. A private-host git URL that matches neither rule lands here — append `.git` to force the clone branch. |
| `dict` | Inline manifest (`name`, `version`, `options`, `sha`). The workspace is the **current process's cwd** — inline file materialisation is not implemented. |

After construction, `agent.agent` is the `ResolvedAgent` — useful for asserting what you got:

```python
agent.agent.name                  # str
agent.agent.harness               # "claude-agent-sdk" | "gitagent"
agent.agent.source                # str | dict | None
agent.agent.model                 # what actually goes on the wire
agent.agent.gateway_type          # "native" | None
agent.agent.gateway_base_url      # str | None
agent.agent.gateway_completions_url
agent.agent.credential_id         # set ⇒ the source is private
agent.agent.agent_type
agent.agent.has_native_gateway    # bool
```

`ResolvedAgent` deliberately holds **no credential**, so printing it can never leak your key.

---

## 5. `options` — what it is and how it merges

`options` is a plain dict of engine options. It is not the final word; it is the **top** layer of a merge:

```
1. build_options()        permission_mode="bypassPermissions", setting_sources=["project"],
                          model=<resolved from the agent document>
2. your options=          merged over (1) — caller wins, including over model
3. identity loader        the agent's own agent.yaml (GAP repo) resolves options...
4. ...then (1)+(2) are overlaid on top of (3) — caller/gateway intent wins
5. loader `harden()`      runs LAST, so compliance constraints declared by the repo
                          (e.g. GAP human-in-the-loop forcing permission_mode="default")
                          can still override you
```

For the `claude-agent-sdk` engine, keys are **filtered against `ClaudeAgentOptions.__dataclass_fields__`** —
an unknown key is silently dropped rather than attached. Commonly used:

| Key | Type | What it does |
|---|---|---|
| `system_prompt` | `str` / preset dict | Replaces / sets the system prompt. |
| `allowed_tools` | `list[str]` | Whitelist, e.g. `["Read", "Glob", "Bash"]`. |
| `disallowed_tools` | `list[str]` | Blacklist. |
| `permission_mode` | `str` | `"bypassPermissions"` (the default this SDK sets), `"default"`, `"acceptEdits"`, `"dontAsk"`, `"auto"`. |
| `max_turns` | `int` | Cap agentic turns. |
| `max_budget_usd` | `float` | Cost cap the CLI itself enforces. |
| `model` | `str` | Override the resolved model. **Usually wrong to set** — see §6, the gateway wants a specific id. |
| `session_id` | `str` | Pin the CLI's session id, which is what makes resuming a *specific* past conversation work. |
| `cwd`, `env`, `session_store`, `include_partial_messages` | — | Set by the harness; your value is only used if the harness has not already defaulted it. |

The full accepted set is every field on `claude_agent_sdk.ClaudeAgentOptions` (45 of them on the pinned
version), including `hooks`, `mcp_servers`, `skills`, `agents`, `settings`, `add_dirs`, `thinking`,
`fallback_model`, `extra_args`.

For the `gitagent` engine the options actually read are `model` (**required** — it raises without one),
`system_prompt`, `max_turns`, `tools`, and `constraints`.

---

## 6. What the agent document supplies (and the three model-routing cases)

You do **not** pass any of these — `GET /v1/agents/{id}` answers them:

| From the document | Becomes |
|---|---|
| `harness` | the engine (`claude-agent-sdk` / `gitagent`) |
| `source` / `sourceUrl` | the workspace to clone (unless you pass `source=`) |
| `model_gateway.binding.externalModelId` → `.model` → top-level `model` | the model string put on the wire |
| `model_gateway.type` / `.baseUrl` | which env branch is built, and where model calls go |
| `credential_id` | a flag that the source is private (the SDK cannot redeem it — see §10) |

For a **native** gateway the model that goes upstream is the binding's `externalModelId`, which is the backing
Lyzr Studio agent id (a bare ObjectId), **not** the catalog name like `"Anthropic:anthropic/claude-sonnet-4-6"`.
Sending the catalog name to the gateway is a 500. This is why overriding `options={"model": ...}` is usually
a mistake.

### The three routing cases

| Case | `ANTHROPIC_BASE_URL` becomes | `llm_proxy_url` |
|---|---|---|
| `claude-agent-sdk` (or any Anthropic-shaped engine) + **native** gateway | `{proxy}/up/{base64url(gateway_base_url)}` | **required** |
| `gitagent` + **native** gateway | `{gateway_base_url}/v4` | not used |
| any engine, **no** gateway | not set — direct Anthropic | not used |

Anthropic-shaped engines are `claude-agent-sdk`, `claude-code`, `deepagents`.

**Why.** `claude-agent-sdk` speaks the Anthropic Messages wire shape and has no OpenAI fallback; a Lyzr native
gateway is an OpenAI `/v4/chat/completions` endpoint. Pointing the Claude CLI straight at it returns **405**.
The proxy translates. `gitagent` needs none of this — it speaks OpenAI natively.

If you hit case 1 without `llm_proxy_url`, you get a `GatewayConfigError` at construction time, not a 405
mid-run.

In case 1 the full env set handed to the engine is:

```
ANTHROPIC_BASE_URL   = {proxy}/up/{b64url(gateway_base_url)}   # no "=" padding, URL alphabet
GITAGENT_MODEL_BASE_URL = {gateway_base_url}
GITCLAW_MODEL_BASE_URL  = {gateway_base_url}
ANTHROPIC_API_KEY    = your api_key      # on a gateway run this holds a Lyzr key, not sk-ant-
OPENAI_API_KEY       = your api_key
LYZR_API_KEY         = your api_key
CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS = 1
```

The last one matters: without it the Claude CLI injects Claude-Code-only experimental betas and a
spec-compliant gateway rejects the call with `400 context_management: Extra inputs are not permitted`.

Your `envs=` are merged **under** these, so you cannot accidentally point a gateway-bound agent somewhere
else. One key is additionally removed when a gateway resolved: `CLAUDE_CODE_USE_BEDROCK`, because the CLI
honours it and ignores `ANTHROPIC_BASE_URL` entirely, which would send the gateway model id to Bedrock.

**Case 3 (no gateway):** the SDK sets nothing, so supply the credential yourself —
`envs={"ANTHROPIC_API_KEY": "sk-ant-…"}`, or rely on an already-authenticated `claude` CLI.

---

## 7. Session stores

`session_store` is how a transcript survives; pass it only if you want resume.

- **`"memory"`** — `InMemorySessionStore`, registered out of the box. Process-local, one shared instance per
  process, lost on exit. Fine for tests and single-process resume.
- **A descriptor** — `{"kind": "<name>", "options": {...}}`. The registry resolves `kind` to a *builder*
  which receives `options`; a bare string means `options=None`.
- **An instance** — any object with callable `append` and `load` (the upstream `claude_agent_sdk`
  `SessionStore` contract; it is duck-typed, never `isinstance`-checked). Passed straight through.

Register your own:

```python
from computeragent.session_stores import register_session_store

register_session_store("mystore", lambda options: MyStore(**(options or {})))
```

> **`mongo` and `http` kinds are being added right now.** Do not hard-code their option shapes from this
> document. Check what is actually registered before relying on them:
> ```python
> from computeragent.session_stores import resolve_session_store
> from computeragent.harness.errors import UnknownSessionStoreError
> try:
>     resolve_session_store("mongo")
> except UnknownSessionStoreError as exc:
>     print(exc)      # the message lists every registered kind
> ```
> An unknown kind raises `UnknownSessionStoreError` naming the registered set, so the registry is
> self-documenting.

The store is handed to the Claude SDK itself, which mirrors transcript batches into it and materialises prior
entries before spawning the subprocess. The harness deliberately does **not** also replay it — doing both
would double the transcript. The store's key is `(project_key, session_id)` and the SDK owns both: resume
within a process works out of the box, but resuming a *specific* past conversation means pinning
`options={"session_id": "<the CLI's uuid>"}`.

---

## 8. Running a turn

`chat()` returns immediately with a `ChatHandle`; nothing is launched until you iterate or await it.

```python
handle = agent.chat("Do the thing.")

# (a) stream
async for event in handle:
    # EngineEvent: .kind in {"session_started","sdk_message","usage_snapshot",
    #                        "permission_request","session_ended"}
    # For "sdk_message", .payload is the raw upstream message object.
    ...

# (b) or just await — drains the stream and returns the ChatResult
result = await handle
```

Both forms compose: a partial iteration followed by `await handle` resumes where iteration left off, and
`result()` is memoised, so awaiting twice returns the same object.

`ChatResult` is a frozen dataclass:

| Field | Type | |
|---|---|---|
| `session_id` | `str` | the harness lifecycle id (`ca_…`) |
| `messages` | `tuple[Any, ...]` | every message the engine produced, in order |
| `usage` | `UsageRollup` | `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `cost_usd` |
| `duration_ms` | `float` | wall time for this turn |

`handle.get_usage()` returns the same `UsageRollup` and is safe to call **mid-stream** — it returns an empty
rollup if the run has not started yet. `claude-agent-sdk` reports usage as *cumulative* (each snapshot
replaces the previous); `gitagent` reports *deltas* (summed).

`await handle.cancel()` sets the cancel event and unblocks the input queue; the engine stops at its next
checkpoint. It is a request, not a kill.

### The `async with` lifecycle

**One `ComputerAgent` instance is one logical session.** `session_started` fires on the first chat and
`session_ended` on dispose, so N `chat()` calls collapse into one conversation rather than N orphan sessions.

```python
async with ComputerAgent(...) as agent:
    await agent.chat("first")
    await agent.chat("second")       # same session, appended as a turn
# dispose(): emits session_ended with cumulative usage, removes any temp clone,
#            flushes telemetry
```

Skipping `async with` (or an explicit `await agent.dispose()`) leaks the temp clone directory, drops the
final `session_ended` event, and loses buffered telemetry.

---

## 9. Policy, permissions and telemetry

**Tool gating**, in the order the harness consults it:

1. `policy_decider` — an async `(tool_name, tool_input, context) -> PermissionResultAllow|Deny|dict`.
   `computeragent.PolicyToolAuthorizer` (backed by `OpaPolicyEngine`, `CedarPolicyEngine` or
   `SrsPolicyEngine` from `computeragent.policy`) already matches this signature.
2. `on_tool_call` — your own async callback, given a `PermissionRequest` (`call_id`, `tool_name`, `input`,
   `risk`), returning `{"behavior": "allow"|"deny", "message": ..., "updated_input": ...}`.
3. Neither set → allow with the original input.

On the `claude-agent-sdk` engine a `policy_decider` is wired into `can_use_tool` **and**, when
`permission_mode` is one of `bypassPermissions` / `dontAsk` / `auto` / `acceptEdits`, additionally into a
`PreToolUse` hook — because `can_use_tool` is skipped entirely under those modes and the policy would go
unenforced. The hook is merged with any hooks you or the repo supplied, never replacing them. A decider that
raises fails **closed**.

Note that `on_tool_call` only fires for engines that emit `permission_request` events — today that is
`gitagent`. On `claude-agent-sdk`, gating goes through `policy_decider`.

**Telemetry** is on by default via a module-level pipeline built from environment variables:

| Env var | Effect |
|---|---|
| `COMPUTERAGENT_OTEL=disabled` | turn the OTel sink off (it is on whenever the `otel` extra is importable) |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SERVICE_NAME` | standard OTLP config; no endpoint → console exporter |
| `AGENTOS_DISCOVERY_URL` / `AGENTOS_INGEST_URL` | attach the AgentOS HTTP sink (needs `httpx`) |
| `COMPUTERAGENT_HARNESS_TOKEN` / `AGENTOS_INGEST_TOKEN` | bearer for that sink |
| `COMPUTERAGENT_CAPTURE_CONTENT`, `COMPUTERAGENT_CAPTURE_CONTENT_MODE`, `COMPUTERAGENT_MAX_ATTRIBUTE_LENGTH`, `COMPUTERAGENT_OTEL_TEMPORALITY` | content capture and attribute limits |

Or configure it in code and pass the result as `telemetry_pipeline=`:

```python
from computeragent import configure, PiiRedactor, GuardrailFilter

pipeline = configure(
    middleware=[PiiRedactor(), GuardrailFilter()],
    sinks=[],                      # [] disables all auto-attachment
    capture_content=True,
)
```

---

## 10. Errors

All of these are raised **from the constructor**, before anything boots.

| Exception | Base | When |
|---|---|---|
| `AgentResolveError` | `RuntimeError` | bad config, transport failure, or an HTTP error from AgentOS. Messages are safe to show a user — the key is never included. |
| `UnsupportedAgentError` | `AgentResolveError` | the agent resolved but its shape is not implemented here. The message always names the offending field. |
| `GatewayConfigError` | `RuntimeError` | the (engine, gateway, proxy) combination cannot produce a working run. |
| `UnknownEngineError` | `HarnessError` | `engine="..."` matched nothing; the message lists the registered engines. |
| `UnknownSessionStoreError` | `HarnessError` | `session_store="..."` matched nothing; the message lists the registered kinds. |

### Agent shapes that are refused

| Shape | Message names | What to do |
|---|---|---|
| `harness` not in `{claude-agent-sdk, gitagent}` (e.g. `deepagents`, a custom TS engine) | `harness` | Pass `engine=` with your own `EngineDriver` to run it anyway, or re-register the agent on a supported harness. |
| `model_gateway.type != "native"` (a **generic** gateway) | `model_gateway.type` | Not implemented — its upstream contract differs. Bind the agent to a native gateway, or to none. |
| `mcp_servers` non-empty | `mcp_servers` | MCP is not supported. The servers would be silently dropped, so the run is refused instead. Unbind them, or run this agent server-side. |
| `orchestration` set | `orchestration` | Sub-agent fan-out is not supported. The agent would run as a plain single agent, which is not what it declares. |
| `modelGatewayId` set but `model_gateway` absent in the response | `did not resolve` | The binding did not resolve **for this caller**. Check that the agent's `ownerGroup` matches a group with a configured gateway. |
| gateway resolved but carries no `baseUrl` | `GatewayConfigError` | Fix the gateway's `baseUrl` in AgentOS. |

A **null** gateway is *not* an error — that is a direct-Anthropic run.

One more resolve-time failure: if the agent document has no `source` and you passed no `source=`, you get
`AgentResolveError("agent … has no source and none was passed — give the run a workspace with
source=Path('...') or register a source on the agent.")`.

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `AgentResolveError: … rejected the credential (401 …)` | A bare Lyzr workspace key authenticates only where the **PAGOS bridge** is configured on the AgentOS deployment. Without it every workspace key 401s. | Have the deployment set `PAGOS_REDIS_URL` / `PAGOS_RESOLVE_URL`. Verify the key itself with a direct `curl -H "x-api-key: …" {base}/v1/agents`. |
| `AgentResolveError: no agent … (404)` | The route wants the **24-hex Mongo `_id`**, not the stable `agentId`. A non-ObjectId is rejected outright. | Take the id from the agent's URL in the SPA, or `id` from the agents list response. |
| `403 NOT_OWNER` | The agent exists but belongs to another group. | The key's groups must include the agent's `ownerGroup`. |
| `403 INSUFFICIENT_PERMISSION` | Key lacks `agents:read`. | Grant it. |
| `AgentResolveError: could not reach AgentOS at … (ConnectError)` | Wrong `base_url`, or the server is down. | Curl the URL from the same shell. Remember all three `base_url` spellings work, so the path is rarely the problem. |
| `GatewayConfigError: … needs the Anthropic-to-OpenAI translating proxy` | Case 1 of §6 without `llm_proxy_url`. | Pass `llm_proxy_url=`; start the proxy if it isn't running. |
| HTTP **405** from the model call | The Claude CLI is POSTing Anthropic Messages at an OpenAI endpoint — `ANTHROPIC_BASE_URL` is not going through the proxy's `/up/<b64>` segment. | Confirm `"/up/" in agent._coordinator._envs["ANTHROPIC_BASE_URL"]`. If not, you are not on the branch you think — check `agent.agent.gateway_type` and the `engine` you passed. |
| **Gateway unreachable** — the turn fails and everything in the SDK looks correct | These gateways are often Cloudflare tunnels whose hostnames go stale, so `gateway_base_url` points at NXDOMAIN. | `curl` both the proxy's `/health` and `agent.agent.gateway_base_url`. If the gateway is dead, the `baseUrl` in AgentOS is stale — fix it there, not in your code. |
| `400 context_management: Extra inputs are not permitted` | The CLI injected Claude-Code-only experimental betas. | `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` is set automatically on the gateway branch — if you see this, check nothing in `envs=` is being lost, and that you are on the gateway branch at all. |
| **Empty completion** — the turn succeeds but no text comes back | Usually the missing betas flag above, or the proxy ordering a text block ahead of a `tool_result`. | Check the flag first, then the proxy's block ordering. |
| `input_tokens` is **0** while `output_tokens` is not | Real and observed, and **not an SDK bug**: the gateway omits prompt tokens when **streaming** (the same call made non-streaming reports them), so there is nothing to map onto `input_tokens`. | Nothing to fix client-side. It does mean per-turn cost is understated. |
| No tool calls at all | An `allowed_tools` list that excludes what the task needs, or a prompt the model can answer without the workspace. | Widen `allowed_tools`; check `permission_mode`. |
| `UnknownEngineError` / `UnknownSessionStoreError` | Typo, or a plugin you meant to register. | The message lists what *is* registered. |
| Temp clone directories piling up | You skipped `async with` / `dispose()`. | Use `async with`. |

### A staged diagnostic

`examples/e2e/live_agent_run.py` is a working, verified invocation whose stages are ordered by dependency, so
a failure localises itself instead of arriving as "it didn't work":

```bash
uv run python examples/e2e/live_agent_run.py \
    --base-url  http://localhost:8788/agentos/api \
    --agent-id  6aa93f1b3817a13d3103d2c1 \
    --api-key   "$LYZR_API_KEY" \
    --llm-proxy-url http://127.0.0.1:8786
```

Every flag falls back to an env var: `AGENTOS_BASE_URL`, `COMPUTERAGENT_AGENT_ID`, `LYZR_API_KEY`,
`LLM_PROXY_URL`, `GIT_TOKEN`. `--source <dir>` runs against a local directory instead of cloning — the
quickest way to sidestep a private source while you debug the model path.

The stages, each of which is a thing that can independently be wrong:

1. resolve the agent from AgentOS → prints name, harness, model, gateway, `ANTHROPIC_BASE_URL`
2. preflight the model path → `GET {proxy}/health` and `GET {gateway_base_url}`
3. materialise the workspace (clone or local dir)
4. load the identity → asserts the resolved model survived the loader merge
5. run one turn against the live model
6. tool calls round-tripped
7. a non-empty reply came back

---

## 12. Known limitations

- **MCP is not supported.** An agent with non-empty `mcp_servers` is refused at resolve time rather than run
  with the servers silently dropped.
- **Sub-agent orchestration is not supported.** An agent with `orchestration` set is refused, because it
  would otherwise run as a plain single agent — not what it declares.
- **Only `native` gateways (or none).** Generic gateways are refused.
- **Only two harnesses run in-process:** `claude-agent-sdk` and `gitagent`. Anything else needs your own
  `EngineDriver` passed as `engine=`.
- **A private source needs `git_token`.** The agent document's `credential_id` is echoed back so you know auth
  is needed, but the SDK **cannot resolve it client-side** — redeeming it requires an operator service token.
  Without `git_token` the SDK tries AgentOS's GitHub-App credential route (best-effort; any miss,
  misconfiguration, or deployment without the App installed falls through silently) and then clones
  anonymously, which fails for a private repo. The `credential_id` only ever appears in the error message.
- **`budget_max_usd` is not enforced** by either built-in engine; it is carried into `EngineContext` and no
  more. Use `options={"max_budget_usd": ...}` for a cap the Claude CLI actually applies.
- **`project_key` is inert** in the current wiring — accepted and stored, but the session-store key is owned
  by the Claude SDK, which derives its project key from the workspace cwd.
- **An inline `dict` source does not materialise files.** The workspace falls back to the process's cwd.
- **`cancel()` is cooperative**, not a kill — the engine stops at its next checkpoint.
- **`gitagent` tool execution is MCP-only**; the engine does not run filesystem tools itself.
