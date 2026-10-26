# Using the SDK

`computer-agent-py` runs an agent that is **registered in AgentOS** inside **your own process** —
typically a sandbox built from an image the agent is baked into.

You name the agent by id. The SDK fetches its definition from AgentOS and reproduces what the AgentOS
dispatch path would have sent to a ComputerAgent server — same model, same engine environment, same
permission mode — except the engine runs in this process.

**Every call the SDK and its engine make goes through an egress proxy.** Each downstream service is
reached at a URL you configure — the agent config, the global RAI policies, SRS, the model, MCP and
telemetry — and there is no direct path to any of them. The proxy routes each URL to its service and
swaps the key the SDK presents for the real one.

---

## 1. Install

Requires **Python ≥ 3.10**.

```bash
pip install 'computer-agent-py[agentos]'
# or, in this repo
uv sync --all-extras --dev
```

`[agentos]` is not optional in practice: it pulls `httpx`, and every call the SDK makes is HTTP.

| Extra | Pulls | Needed for |
|---|---|---|
| *(base)* | `claude-agent-sdk`, `typing-extensions` | the `claude-agent-sdk` engine |
| `agentos` | `httpx` | **resolving the agent, the guardrails and the events sink** (required) |
| `otel` | `opentelemetry-*` | OTLP telemetry to `telemetry_url` |
| `gitagent` | `openai`, `anthropic`, `pyyaml` | the `gitagent` engine |
| `gap` | `pyyaml` | GAP mode (`gap_dir`) |
| `mongo` | `pymongo` | the `mongo` session store |
| `cedar` | `cedarpy` | `CedarPolicyEngine` |
| `all` | all of the above | everything |

The `claude-agent-sdk` engine spawns a Claude Code CLI subprocess, which **ships bundled inside
`claude-agent-sdk`** — no `npm install` is needed.

---

## 2. Quickstart

In a sandbox every setting comes from the environment, so construction takes no arguments:

```bash
export COMPUTERAGENT_AGENT_ID=6aa93f1b3817a13d3103d2c1
export COMPUTERAGENT_ENGINE=claude-agent-sdk
export COMPUTERAGENT_BASE_URL=http://egress/agentos/api
export COMPUTERAGENT_GUARDRAILS_URL=http://egress/guardrails
export COMPUTERAGENT_LLM_URL=http://egress/llm
export COMPUTERAGENT_MCP_URL=http://egress/mcp
export LYZR_API_KEY=...          # injected when the sandbox is created
```

```python
import asyncio
from computeragent import ComputerAgent

async def main():
    async with ComputerAgent() as agent:
        result = await agent.chat("List the files in your working directory.")
        print(result.messages[-1])
        print(result.usage)

asyncio.run(main())
```

Every setting can equally be passed as an argument, which wins over its variable:

```python
async with ComputerAgent(
    agent_id="6aa93f1b3817a13d3103d2c1",
    engine="claude-agent-sdk",
    base_url="http://egress/agentos/api",
    guardrails_url="http://egress/guardrails",
    llm_url="http://egress/llm",
    mcp_url="http://egress/mcp",
    api_key=os.environ["LYZR_API_KEY"],
    options={"system_prompt": "You are terse.", "disallowed_tools": ["Write", "Edit"]},
    session_store="memory",
    budget_max_usd=0.50,
) as agent:
    handle = agent.chat("Summarise the repo layout.")
    async for event in handle:
        print(event.kind)
    result = await handle
```

`ComputerAgent(...)` resolves its settings and the agent **synchronously in the constructor**, so a
missing setting, a bad id, a bad key or an unsupported agent shape raises before anything runs.

---

## 3. Settings

Each setting comes from its argument or, when that is unset or blank, its environment variable. Every
missing required setting is reported in **one** `SettingsError` naming both spellings, before any
request is made. A URL setting must be an `http(s)` URL.

| Argument | Environment variable | Required | What it is |
|---|---|---|---|
| `agent_id` | `COMPUTERAGENT_AGENT_ID` | **yes** | The agent's **24-hex Mongo `_id`** in AgentOS (the id in the agent's URL). Not the stable `agentId` field. |
| `engine` | `COMPUTERAGENT_ENGINE` | **yes** | `"claude-agent-sdk"` or `"gitagent"`; an `EngineDriver` instance only as an argument. |
| `api_key` | `LYZR_API_KEY` | **yes** | The key the SDK presents on every call as `x-api-key`. In a sandbox it is injected at creation and the egress proxy swaps it for the real one. Read from the environment **at each use**, so it can be replaced while the agent runs. |
| `base_url` | `COMPUTERAGENT_BASE_URL` | **yes** | Route to the AgentOS agent config: the model and its MCP bindings. |
| `guardrails_url` | `COMPUTERAGENT_GUARDRAILS_URL` | **yes** | Route to the guardrails check, for the user's message and every tool call. |
| `llm_url` | `COMPUTERAGENT_LLM_URL` | **yes** | Route for every model call the engine makes. |
| `mcp_url` | `COMPUTERAGENT_MCP_URL` | **yes** | Route for MCP: each server bound to the agent becomes `{mcp_url}/{server_id}`. |
| `telemetry_url` | `COMPUTERAGENT_TELEMETRY_URL` | no | Route for OTLP traces, metrics and logs. Unset: no OTLP export. |
| `gap_dir` | `COMPUTERAGENT_GAP_DIR` | no | Turns on GAP mode (§6). |

The requests made from each route, all with `x-api-key: <api_key>`:

| Route | Requests |
|---|---|
| `base_url` | `GET {base_url}/v1/agents/{agent_id}` once, at construction |
| `guardrails_url` | `POST {guardrails_url}/v1/check`: one `input` check per text block of the user's message, one `tool-input` check per tool call |
| `llm_url` | the engine's own model calls (`/v1/messages`, `/chat/completions`) |
| `mcp_url` | the engine's MCP calls |
| `telemetry_url` | `POST /v1/traces`, `/v1/metrics`, `/v1/logs` |

### `base_url` — three accepted spellings

`api_base()` normalises `base_url` to the `/v1` API root, so a trailing slash or an already-versioned
base never doubles the path:

| You pass | Resolves to |
|---|---|
| `http://host:8788` | `http://host:8788/agentos/api/v1` |
| `http://host:8788/agentos/api` | `http://host:8788/agentos/api/v1` |
| `http://host:8788/agentos/api/v1` | unchanged |
| `http://egress/computer-agent-api/agentos/api` (behind the egress proxy) | `http://egress/computer-agent-api/agentos/api/v1` |

---

## 4. Constructor reference

All parameters are **keyword-only**. The settings in §3 are not repeated here.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `options` | `Mapping[str, Any] \| None` | `None` | Engine options; merged over what the agent resolves, caller wins (§5). |
| `envs` | `Mapping[str, str] \| None` | `None` | Extra environment for the engine subprocess. The model-route envs are layered **over** yours. |
| `session_store` | `str \| SessionStore \| Mapping \| None` | `None` | `"memory"`, a descriptor `{"kind": ..., "options": {...}}`, or a store instance (§7). |
| `session_id` | `str \| None` | generated `ca_…` | Pins the **telemetry** lifecycle session id. |
| `agent_name` | `str \| None` | agent document's `name` | Telemetry label only. |
| `agent_description` | `str \| None` | `None` | Telemetry label only. |
| `policy_decider` | `PolicyDecider \| None` | the SRS gate | Your own tool gate; **replaces** the SRS tool-call gate (§9). The input check still runs. |
| `on_tool_call` | `Callable[[PermissionRequest], Awaitable[dict]] \| None` | `None` | Per-tool callback for engines that emit permission requests (`gitagent`). |
| `budget_max_usd` | `float \| None` | `None` | Maps to the `max_budget_usd` option the Claude CLI enforces. |
| `telemetry_pipeline` | `Pipeline \| None` | built from `telemetry_url` | Your own pipeline replaces the one built from the routes (§9). |

After construction, `agent.agent` is the `ResolvedAgent`:

```python
agent.agent.name             # str
agent.agent.harness          # "claude-agent-sdk" | "gitagent"
agent.agent.model            # what goes on the wire
agent.agent.mcp_mode         # "engine" | "studio"
agent.agent.mcp_bindings     # tuple[McpBinding, ...]
```

`ResolvedAgent` holds **no credential**, so printing it can never leak your key; neither does the
resolved settings object.

---

## 5. `options` — what it is and how it merges

`options` is a plain dict of engine options. It is not the final word; it is the **top** layer of a merge:

```
1. build_options()        permission_mode="bypassPermissions", setting_sources=["project"],
                          model=<resolved from the agent document>
2. your options=          merged over (1) — caller wins, including over model
3. identity loader        in GAP mode, the agent.yaml in gap_dir resolves options...
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
| `allowed_tools` | `list[str]` | A permission **pre-approval** list. It blocks nothing, in any mode — not a security boundary. See below. |
| `disallowed_tools` | `list[str]` | Blacklist. |
| `permission_mode` | `str` | `"bypassPermissions"` (the default this SDK sets), `"default"`, `"acceptEdits"`, `"dontAsk"`, `"auto"`. |
| `max_turns` | `int` | Cap agentic turns. |
| `max_budget_usd` | `float` | Cost cap the CLI itself enforces. |
| `model` | `str` | Override the resolved model. **Usually wrong to set** — see §6. |
| `session_id` | `str` | Pin the CLI's session id, which is what makes resuming a *specific* past conversation work. |
| `cwd`, `env`, `session_store`, `include_partial_messages` | — | Set by the harness; your value is only used if the harness has not already defaulted it. |

The full accepted set is every field on `claude_agent_sdk.ClaudeAgentOptions` (45 of them on the pinned
version), including `hooks`, `mcp_servers`, `skills`, `agents`, `settings`, `add_dirs`, `thinking`,
`fallback_model`, `extra_args`.

For the `gitagent` engine the options actually read are `model` (**required** — it raises without one),
`system_prompt`, `max_turns`, `tools`, and `constraints`.


### Restricting tools: `allowed_tools`, `disallowed_tools`, `policy_decider`

These are not three spellings of the same idea, and picking the wrong one is the most common way
to believe you have a sandbox when you do not. Verified against a live agent.

**`allowed_tools` is a pre-approval list, not a whitelist.** It names tools that may run *without
prompting*; it never blocks anything. Under `permission_mode="bypassPermissions"` — what this SDK
dispatches, matching AgentOS — nothing prompts at all, so it does nothing whatsoever. The engine
logs a warning when it sees both:

```
allowed_tools is set alongside permission_mode='bypassPermissions', which skips permission
prompts — the allowlist cannot restrict anything there and every tool stays available.
```

Switching to `permission_mode="default"` does **not** rescue it. In that mode `can_use_tool` fires
only when the CLI's own permission rules evaluate to *ask*, and those rules read settings this SDK
does not control. A live run with `permission_mode="default"`, `allowed_tools=["Read","Glob",
"Grep"]` and a decider that denies everything else invoked `Bash` anyway and never consulted the
decider once. **Do not use `allowed_tools` as a security boundary.**

**`disallowed_tools` is a blacklist** and is enforced in every mode. For "stop this agent writing",
it is one line and has no configuration that silently re-opens the tool:

```python
options={
    "system_prompt": "You are terse.",
    "disallowed_tools": ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"],
}
```

**`policy_decider` is the real gate.** By default it is the SDK's SRS gate (§9), which checks every
tool call against the agent's RAI policies. Passing your own **replaces** that gate — the input check
still runs. Under a prompt-skipping mode the engine routes it through a
`PreToolUse` hook, which fires for *every* tool call regardless of permission mode — so keep the
default `bypassPermissions` and decide in code:

```python
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

READ_ONLY = {"Read", "Glob", "Grep"}

async def read_only(tool_name, tool_input, context):
    if tool_name in READ_ONLY:
        return PermissionResultAllow(
            behavior="allow", updated_input=tool_input, updated_permissions=None
        )
    return PermissionResultDeny(
        behavior="deny", message=f"{tool_name} is not read-only", interrupt=False
    )

async with ComputerAgent(
    base_url=..., agent_id=..., api_key=...,
    engine="claude-agent-sdk",
    options={"system_prompt": "You are terse."},   # keep bypassPermissions
    policy_decider=read_only,
) as agent:
    ...
```

Asked to run `whoami`, that agent attempted `Bash` eleven times and was denied every time; the
decider saw all eleven calls — including one `Agent` call where the model tried to route around the
denial through a sub-agent. It also sees `tool_input`, so it can decide on paths and arguments,
which no tool-name list can.

| You want | Use |
|---|---|
| Block specific tools | `disallowed_tools` — any mode, one line |
| A read-only agent, enforced | `policy_decider` under the default `bypassPermissions` |
| Decisions on paths or arguments | `policy_decider` — it sees `tool_input` |
| Fewer prompts in an interactive run | `allowed_tools` — its actual purpose |

---

## 6. What the agent document supplies

You do **not** pass any of these — `GET {base_url}/v1/agents/{id}` answers them:

| From the document | Becomes |
|---|---|
| `model_gateway.binding.externalModelId` → `.model` → top-level `model` | the model string put on the wire |
| `mcp_mode`, `mcp_servers` | the MCP servers attached to the engine (below) |

The document's `source` and gateway URL are **not** used: the agent is baked into the image, and
every model call goes to `llm_url`.

### Where the agent comes from

The agent is baked into the image; nothing is cloned or fetched. Its definition is read once, at
construction, from `GET {base_url}/v1/agents/{agent_id}`; behind the egress proxy that is
`GET http://egress/computer-agent-api/agentos/api/v1/agents/{agent_id}`.

- **Default:** the engine runs in the process's working directory (`options={"cwd": ...}` overrides
  it) and the agent is defined by your `options`.
- **GAP mode** (`gap_dir`): the SDK loads the agent from that directory's `agent.yaml`, `SOUL.md`,
  `RULES.md`, skills, hooks, sub-agents and script tools. The directory must exist and contain
  `agent.yaml`, or construction fails. GAP mode is never inferred: an `agent.yaml` that happens to sit
  in the working directory does nothing without `gap_dir`. `mcp_servers` declared in `agent.yaml` are
  refused, because they name their own URLs and would bypass the egress proxy — bind MCP servers to
  the agent in AgentOS instead.

### Model calls

Every engine gets the same environment:

```
ANTHROPIC_BASE_URL       = llm_url
GITCLAW_MODEL_BASE_URL   = llm_url
GITAGENT_MODEL_BASE_URL  = llm_url
ANTHROPIC_API_KEY = OPENAI_API_KEY = LYZR_API_KEY = api_key
CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS = 1
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = 1
DISABLE_AUTOUPDATER = 1
```

`llm_url` is passed verbatim: whatever sits behind it is the egress proxy's choice. The last two
variables stop the Claude CLI's own background traffic (telemetry, error reporting, update checks),
which would otherwise go straight to Anthropic and Datadog. `CLAUDE_CODE_USE_BEDROCK` is removed from
your `envs=`, because the CLI would route through Bedrock and ignore `ANTHROPIC_BASE_URL`.

For a native gateway the model is the binding's `externalModelId` (the backing Lyzr Studio agent id),
not the catalog name — which is why overriding `options={"model": ...}` is usually a mistake.

### MCP servers

With `mcp_mode` `"engine"` (the default), each binding becomes an engine MCP server:

```python
mcp_servers[tool_name] = {
    "type": "http",
    "url": f"{mcp_url}/{server_id}?tools={action_names}",
    "headers": {"x-api-key": api_key},
}
```

- `tool_name` is the server name, so the model sees `mcp__<tool_name>__<tool>` whoever runs it.
- A "require user sign-in" binding (`persist_auth: false`) uses the caller's own server, which AgentOS
  returns as `your_server_id`. If the caller has not connected one, that tool is withheld and a
  warning names it.
- `allowed_tools` gets `mcp__<tool_name>` on Anthropic-shaped engines only; gitagent treats
  `allowed_tools` as a strict allowlist.
- With any other `mcp_mode`, Studio runs the tools and nothing is attached.

They are added to the servers the identity loader resolved (a GAP agent's `gap_tools` server stays);
an `mcp_servers` key in your own `options` still wins.

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

Two more kinds ship with the SDK:

| Kind | Options | Notes |
|---|---|---|
| `mongo` | `url` **(required)**, `database`, `collection`, `owner_user`, `owner_group` | One document per transcript, uuid-deduped. Warns loudly when `database` is unset — the fallback is *not* the database AgentOS reads. Needs the `[mongo]` extra. |
| `http` | `base_url` **(required)**, `token` **(required)**, `append_timeout`, `load_timeout`, `auto_refresh` | Two verbs against the CAS, which does the write in-pod — for a harness that cannot hold a database credential. `load` never raises: a store outage costs a session its resume, not its life. |

An unknown kind raises `UnknownSessionStoreError` naming the registered set, and a known kind
missing a required option raises `ValueError` naming the option — the registry is self-documenting.

The store is handed to the Claude SDK itself, which mirrors transcript batches into it and materialises prior
entries before spawning the subprocess. The harness deliberately does **not** also replay it — doing both
would double the transcript.

**Multi-turn works within one `ComputerAgent`.** Each turn after the first resumes the previous turn's
engine session automatically, so the agent remembers earlier turns whether or not a store is configured.
To resume a conversation in a *later process*, pass a durable store plus the engine session id from the
earlier run — read it off any message (`result.messages[-1].session_id`) and pass it back as
`options={"resume": "<that id>"}`. Use `resume`, not `session_id`: the CLI refuses an id that already
exists.

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
`session_ended` on dispose, carrying the summed usage of every turn, so N `chat()` calls collapse into one conversation rather than N orphan sessions.

```python
async with ComputerAgent(...) as agent:
    await agent.chat("first")
    await agent.chat("second")       # same session, appended as a turn
# dispose(): emits session_ended with cumulative usage, flushes telemetry and
#            closes the pipeline the agent built
```

Skipping `async with` (or an explicit `await agent.dispose()`) drops the final `session_ended` event
and loses buffered telemetry.

---

## 9. Guardrails, permissions and telemetry

### Guardrails

Every check is `POST {guardrails_url}/v1/check`. The egress proxy forwards it to the agent gateway,
which applies the org's RAI policies and then the agent's own; the SDK reads no policies and sends no
policy ids, so a policy change applies from the next check.

| Check | When | Body | On a block |
|---|---|---|---|
| Input | before the turn, on each text block of the user's message | `{"op": "input", "subject": {"text": …}}` | the engine never starts; the turn ends with the reason as the reply |
| Tool call | before every tool call | `{"op": "tool-input", "subject": {"toolCalls": [{"id", "name", "arguments"}]}}` | the tool is denied with the gateway's reason |

- **Masking:** on `rewrite` the gateway's masked text (`[EMAIL]`) is what the engine receives; it is
  not restored.
- **Tool ids:** a tool call is identified by the engine's own tool-use id. A call that reaches the
  gate without one is denied (`tool call has no id`).
- **Tool names:** tool calls are presented as `<engine>__<tool>` (`claude-agent-sdk__Bash`,
  `gitagent__write`), and MCP tools as `mcp__<server>__<tool>` from either engine.
- **No verdict** (unreachable, timed out after 5 s, non-2xx or a malformed answer): a tool is denied
  and an input is blocked. Fail-open is the gateway's decision; when it fails open it answers `allow`
  with `skipped: true`, which the SDK logs.
- A **streamed** (async-iterable) message is read entry by entry and every entry is checked before
  the engine starts; one blocked entry blocks the turn.

Every decision is emitted as a `policy_decision` telemetry event (input checks with
`stage: "llm_input"`) carrying the outcome and the policy that acted — never the text.

Model calls *inside* a turn (the engine sending tool results back to the model) go to `llm_url` and
are not checked by the SDK; guarding them is the job of whatever the egress proxy routes `llm_url` to.

### Your own tool gate

`policy_decider` replaces the SRS tool-call gate — the input check still runs. On the
`claude-agent-sdk` engine a decider is wired into `can_use_tool` and, under the prompt-skipping
permission modes this SDK uses, into a `PreToolUse` hook that fires for every tool call. A decider that
raises fails **closed**. `on_tool_call` fires only on engines that emit permission requests
(`gitagent`).

### Telemetry

A `ComputerAgent` builds its pipeline from its telemetry route:

| Route | Sink | Auth |
|---|---|---|
| `telemetry_url` | OTLP (`OtelSink`, needs the `otel` extra) | `x-api-key`, read when the agent is built |

An unset `telemetry_url` turns telemetry off. There is no AgentOS dashboard events sink. `AGENTOS_DISCOVERY_URL`, `AGENTOS_INGEST_URL`, `AGENTOS_OTEL_URL`
and `OTEL_EXPORTER_OTLP_ENDPOINT` are **not** consulted, so telemetry never goes around the egress
proxy. Content capture and attribute limits still come from `COMPUTERAGENT_CAPTURE_CONTENT`,
`COMPUTERAGENT_CAPTURE_CONTENT_MODE`, `COMPUTERAGENT_MAX_ATTRIBUTE_LENGTH` and
`COMPUTERAGENT_OTEL_TEMPORALITY`. Pass `telemetry_pipeline=` to use your own pipeline instead; a
pipeline the agent built is closed on dispose.

---

## 10. Errors

All of these are raised **from the constructor**, before anything runs.

| Exception | Base | When |
|---|---|---|
| `SettingsError` | `AgentResolveError` | a required setting is missing, or a URL setting is not `http(s)`. Names the argument and the variable. |
| `AgentResolveError` | `RuntimeError` | a bad `gap_dir`, a transport failure, or an HTTP error from AgentOS. Never includes the key. |
| `UnsupportedAgentError` | `AgentResolveError` | the agent resolved but its shape is not implemented here; names the field. |
| `GatewayConfigError` | `RuntimeError` | `llm_url` cannot be used as a model route. |
| `UnknownEngineError` | `HarnessError` | `engine` matched nothing; lists the registered engines. |
| `UnknownSessionStoreError` | `HarnessError` | `session_store` matched nothing; lists the registered kinds. |

### Agent shapes that are refused

| Shape | Message names | What to do |
|---|---|---|
| `harness` not in `{claude-agent-sdk, gitagent}` | `harness` | Pass your own `EngineDriver` as `engine`. |
| `model_gateway.type != "native"` | `model_gateway.type` | Bind the agent to a native gateway, or none. |
| an `mcp_servers` binding with no `server_id` or `tool_name` | `mcp_servers[i]` | Fix the binding in AgentOS. |
| `orchestration` set | `orchestration` | Sub-agent fan-out is not supported. |
| `modelGatewayId` set but `model_gateway` absent | `did not resolve` | The binding did not resolve for this caller; check the agent's `ownerGroup`. |
| GAP mode with `mcp_servers` in `agent.yaml` | the server names | Bind those servers to the agent in AgentOS. |

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `SettingsError: missing required settings: …` | A route or the key is not set. | Set each named argument or environment variable. |
| `AgentResolveError: … rejected the credential (401 …)` | The key did not authenticate at AgentOS. | Check the key the egress proxy swaps in, and that AgentOS has its PAGOS bridge configured. |
| `AgentResolveError: no agent … (404)` | `agent_id` is not the 24-hex Mongo `_id`. | Take the id from the agent's URL in AgentOS. |
| Every turn ends with `[Content blocked: RAI policy check failed: guardrails …]` | The guardrails check reached no verdict. | Check the `guardrails_url` route on the egress proxy; the status and error code follow `guardrails`. |
| Every tool call is denied with `guardrails 4xx` / `guardrails unreachable` | The `guardrails_url` route or the key's permission; a `400` is a body the SDK got wrong. | Check the route; report a `400`. |
| A tool you expected is missing | A sign-in MCP binding the caller has not connected (logged as withheld), or `mcp_mode` is not `"engine"`. | Connect the account in Studio, or check `agent.agent.mcp_mode`. |
| `400 context_management: Extra inputs are not permitted` | The Claude CLI injected experimental betas. | `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` is set automatically; check nothing in `envs=` is lost. |
| `UnknownEngineError` / `UnknownSessionStoreError` | Typo, or a plugin you meant to register. | The message lists what *is* registered. |

### A staged diagnostic

`examples/e2e/live_agent_run.py` runs one turn with every setting taken from the environment, and
prints a pass/fail line per stage — resolve, the routes, the turn's policies, the identity, a live
turn, tool calls, the reply — so a failure localises itself.

---

## 12. Known limitations

- **Sub-agent orchestration is not supported**; an agent with `orchestration` set is refused.
- **Only `native` gateways (or none).** Generic gateways are refused.
- **Only two harnesses run in-process:** `claude-agent-sdk` and `gitagent`.
- **Model calls inside a turn are not checked by the SDK**; see §9.
- **Concurrent `chat()`s on one instance are refused.** Run concurrent work as separate instances.
- **A turn has no timeout and nothing retries.** Impose your own with `asyncio.wait_for`.
- **`cancel()` is cooperative**, not a kill.
- **`gitagent` tool execution is MCP-only.**

---

## 13. Public API and imports

Everything you need to name in code is importable from the top-level package — `from computeragent
import …`. You never need a private path.

| Name | What it is |
|---|---|
| `ComputerAgent` | The client. |
| `ChatHandle`, `ChatResult`, `UsageRollup` | Turn handle and results. |
| `EngineEvent` | What the stream yields. |
| `ResolvedAgent` | What `agent.agent` is. |
| `AgentResolveError`, `SettingsError`, `UnsupportedAgentError`, `GatewayConfigError`, `UnknownEngineError`, `UnknownSessionStoreError`, `HarnessError` | Construction-time failures. `SettingsError` and `UnsupportedAgentError` subclass `AgentResolveError`. |
| `EngineDriver`, `SessionStore`, `PolicyDecider`, `PermissionRequest` | Extension-point protocols. |
| `Pipeline`, `configure`, `PolicyToolAuthorizer` | Telemetry and policy. |

### Getting the assistant's reply out

`result.messages[-1]` is a `ResultMessage`, whose `repr` is metadata — the text is in `.result`.

```python
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

def reply_text(result) -> str:
    for m in reversed(result.messages):
        if isinstance(m, ResultMessage) and isinstance(m.result, str) and m.result:
            return m.result
        if isinstance(m, AssistantMessage):
            text = "".join(b.text for b in m.content if isinstance(b, TextBlock))
            if text:
                return text
    return ""
```

An empty completion surfaces as a `ResultMessage` with an empty `.result` — not an exception.

### `EngineEvent`

`kind` is `"sdk_message"` or `"usage_snapshot"` (the only two the claude engine emits; treat the set as
open). `payload` carries the raw upstream message for `sdk_message`. The token and cost fields are
populated on `usage_snapshot`.

### Async, threads, concurrency

The constructor's AgentOS resolve is a **blocking** HTTP call — in an async server, build with
`await asyncio.to_thread(ComputerAgent, …)`. There is no sync facade; wrap with `asyncio.run`. Several
agents per process are fine; concurrent `chat()`s on one instance are not supported (one instance is one
session with one input queue). Not thread-safe.

### Timeouts

The resolve has a 15-second timeout. **A chat turn has none, and nothing retries.** Behind a request
handler, impose your own: `await asyncio.wait_for(agent.chat(msg), timeout=120)`. `cancel()` is
cooperative and cannot enforce a deadline on a wedged turn.

### Telemetry

`capture_content` defaults to **False**. Without `telemetry_url`, an agent
sends no telemetry at all. Nothing is written to your console unless you build your own pipeline with
`configure(exporter="console")` and pass it as `telemetry_pipeline=`.
