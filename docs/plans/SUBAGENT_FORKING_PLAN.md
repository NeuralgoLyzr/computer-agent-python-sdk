# Sub-agent forking / orchestration — implementation plan (Python SDK)

**Status:** plan only. No implementation code has been written; no source file has been changed.
**Target repo:** `/Users/vraj-parikh/Documents/Lyzr/computer-agent-python-sdk`
**Ported from:** `/Users/vraj-parikh/Documents/Lyzr/computeragent-enterprise` (`packages/harness-server`, `packages/protocol`, `packages/identity-gitagentprotocol`)
**Baseline assumed:** the single-run-mode collapse — `ComputerAgent(base_url, agent_id, api_key)` resolves `GET {base}/v1/agents/{id}` and runs the agent locally through `HarnessCoordinator` + `ClaudeAgentEngine`. The resolved agent doc carries an `orchestration` field (`null` for a plain agent). `src/computeragent/harness/fork.py` is deleted as part of that collapse.

Every claim about TypeScript behaviour below cites `file.ts:line`. Every Python change names the exact file and function.

---

## 0. TL;DR of the decisions taken

| Question | Decision |
|---|---|
| Revive `harness/fork.py`? | **Supersede it, borrow four things from it.** See §1. |
| Worker execution model | **In-process sibling `HarnessCoordinator`s**, not child OS processes. See §2. |
| Isolation | Real git worktrees, ported CLI-for-CLI from `worktree.ts`. See §3. |
| Glob engine for scope guard | `wcmatch.glob.globmatch(..., GLOBSTAR \| DOTGLOB)`, **not** `fnmatch`. See §3.6. |
| Turn lifecycle | Hold back the `None` sentinel; withhold the primary `ResultMessage` from the consumer; run the wave inside `_stream_engine_events`; enqueue the synthesis prompt; close after the synthesis result. Two watchdogs. See §5. |
| Wave events | Five new `EngineEvent` kinds on the `ChatHandle` stream. **Not** forwarded to AgentOS ingest — `KNOWN_KINDS` would silently drop them. See §6. |
| New package | `src/computeragent/harness/orchestration/` — 15 modules, one per TS file. See §7. |

---

## 1. What happens to `harness/fork.py`

I read all 409 lines of `src/computeragent/harness/fork.py` and all 300 lines of `tests/test_fork.py`.

**Verdict: supersede. Delete the module as planned, but lift four specific mechanisms into the new package rather than re-deriving them.**

### 1.1 Why it cannot be revived as-is

`fork.py` is an `mcp__fork__spawn` tool that constructs a whole new `ComputerAgent` per child (`fork.py:361-375`) and awaits its full run (`fork.py:384`). Five properties make it the wrong substrate for orchestration:

1. **It cannot share the parent's `PolicyDecider` instance.** The TS wave passes the orchestrator's *decider object* into every worker (`create-session.ts:182-192`, `spawn-child.ts:165`, `worktree-dispatch.ts:196-198`), which is what makes the guardrail unescapable by fanning out and costs exactly one SRS policy fetch for the whole wave (Python's `SrsPolicyEngine` caches the fetched policy on the instance — `src/computeragent/policy/srs.py:20,158`). `fork.py` passes no `policy_decider` at all to the child (`fork.py:361-375`): **every forked child today runs completely ungated.** A separate process could at best be handed credentials and re-fetch, i.e. N fetches per wave and N chances to fail closed.
2. **It cannot take a git worktree cheaply.** Each child boots its own substrate into a fresh `tempfile.mkdtemp` (`fork.py:348`) or shares the parent workspace verbatim (`fork.py:345-346`) — there is no third option, and "share the parent workspace" is exactly the concurrent-write disaster worktrees exist to prevent.
3. **Fan-out width is model-controlled, not code-controlled.** `max_concurrency` (`fork.py:104`) is a semaphore that throttles, but the *model* decides how many `spawn` calls to make and when. Roster mode's whole point is that the roster is parsed once and dispatched by code with a fixed pool (`spawn-child.ts:222-248`, `worktree-dispatch.ts:146-268`) — neuron's `max_parallel` guarantee.
4. **Budget cannot be shared.** `BudgetAllocator.slice(n)` (`budget.ts:64-67`) needs the wave width `n` up front. A tool the model calls one at a time never knows `n`.
5. **Under single-run mode, each child would re-resolve the agent.** The child's construction path (`fork.py:361`) goes through `ComputerAgent.__init__` → `_ensure_booted` → identity load. In the collapsed world that means an extra `GET {base}/v1/agents/{id}` and an extra substrate boot per worker, for a worker whose identity is a 2-line synthetic stub (`worktree-dispatch.ts:313-319`).

A sixth, softer reason: `fork.py`'s `auto` mode sets `opts["agents"] = {}` and disallows `Task` (`coordinator.py:229-236`). That is the opposite of what GAP's classification says — an embedded `agents/<n>.yaml` with an inline `prompt` **is** meant to be a native in-context Task sub-agent, and only a `use:`-bearing file becomes a worker (`subagents.ts:4-16,131-138`). The port must preserve both, so the auto-steer has to go anyway.

### 1.2 What the port borrows from it, verbatim in spirit

| Borrowed | From | Becomes |
|---|---|---|
| The caps dataclass shape — `max_depth`, `max_children`, `max_concurrency`, `child_timeout_s` | `fork.py:64-81` (`ForkConfig`) | `orchestration/spec.py: OrchestrationLimits`. `max_parallel` replaces `max_concurrency` as the *primary* knob (the spec field, `orchestration.ts:99`); the other three survive as SDK-side safety rails the TS stack lacks. `max_depth` in particular is net-new value: nothing in the TS subsystem stops a worker that is itself an orchestrator from fanning out again. |
| Parent-linkage stamping at the emit chokepoint | `coordinator.py:588-600` (`_emit` stamps `parent_session_id` / `parent_agent_id`), serialized at `telemetry/sinks/agentos_http.py:149` | Kept **unchanged**. This is already exactly TS's `SessionLinkage` (`spawn-child.ts:106-115`) and it is why worker sessions reach AgentOS ingest correctly today. See §6.3. |
| In-process MCP server injection that deep-merges instead of clobbering `mcp_servers` | `coordinator.py:210-240` + `fork.py:217-272` (`build_fork_mcp_server`) | `orchestration/dispatch_tool.py` — supervised mode's `mcp__orchestration__dispatch_many` is injected by the exact same mechanism, including the "only extend an existing `allowed_tools`, never introduce one" rule (`coordinator.py:224-227`), which is the same rule `worker-mcp.ts:158-173` states independently. |
| Guaranteed teardown of live children on `dispose` | `fork.py:164-169` (`ForkContext.teardown`), called at `coordinator.py:250-252` | `orchestration/runner.py: WaveHandle.cancel()`, called from `HarnessCoordinator.dispose` and from `ChatHandle.cancel()`. |

`tests/test_fork.py` is deleted with the module, but three of its patterns are carried into the new suites (§9): the `_FakeEngine` / `_FakeSubstrate` / `_FakeLoader` triple (`tests/test_fork.py:33-56`), `_CapturePipeline` (`tests/test_fork.py:59-68`), and the "assert the cap at the handler boundary before any child is built" style (`tests/test_fork.py:225-268`).

---

## 2. The spec

### 2.1 `OrchestrationSpec` field by field

From `packages/protocol/src/orchestration.ts:96-113`, with defaults from `identity-gitagentprotocol/src/loader.ts:86-105` (`buildOrchestration`).

| Field | Type | Default | Source of default | Notes |
|---|---|---|---|---|
| `mode` | `"roster" \| "supervised"` | `"roster"` | `loader.ts:94` | `orchestration.ts:56-60` |
| `maxParallel` | `int > 0` | `4` | `loader.ts:95` | Clamped at dispatch: `max(1, min(maxParallel, len(entries)))` — `worktree-dispatch.ts:146`, `spawn-child.ts:222` |
| `collect` | `"into-context" \| "gate"` | `"into-context"` | `loader.ts:96` | `orchestration.ts:63-67` |
| `isolation` | `"worktree" \| "none"` | `"worktree"` | `loader.ts:97` | Note: **worktree is the default**, so the git path is the common path, not the exotic one |
| `budgetUsd` | `float ≥ 0` | absent → falls back to `runtime.budget_usd` | `loader.ts:92`, `manifest.ts:57` | Absent on both ⇒ no budget object is created at all (`roster-run.ts:218`, `worktree-dispatch.ts:108`) |
| `workerMcp` | `"inherit" \| "none" \| list[str]` | `"inherit"` | `loader.ts:101` | **Never read directly** — always through `resolveWorkerMcp` (`worker-mcp.ts:33-38`), because absent means `"inherit"`, not `"none"` (`orchestration.ts:104-109`) |
| `workers` | `WorkerCrewEntry[]` | `[]` | `loader.ts:103` | Empty when only inline/embedded sub-agents exist |

`buildOrchestration` returns `undefined` — i.e. a plain agent — only when there is **no** `orchestration:` block **and** no `use:` worker (`loader.ts:91`). Either alone produces a spec.

`WorkerCrewEntry` (`orchestration.ts:39-53`), built by `toWorkerEntry` (`subagents.ts:167-184`):

| Field | YAML key | Default |
|---|---|---|
| `name` | file basename | required |
| `use` | `use:` | required (its presence is what makes the file a worker) |
| `separateSession` | `separate_session:` | **`true`** (`subagents.ts:176`) — referenced workers default to their own `session_id` |
| `scope` | `scope: {write: [], read: []}` | omitted when both halves absent (`subagents.ts:177`) |
| `budgetUsd` | `budget_usd:` | absent |
| `maxTurns` | `max_turns:` | absent |
| `model` | `model:` | absent |
| `toolSubset` | `tools:` | absent |
| `mcp` | `mcp:` | absent ⇒ the spec's `workerMcp` decides |

### 2.2 The crew classification rule

`classifyGapAgents` (`subagents.ts:97-143`) walks `agents/` and applies one rule that must survive the port exactly:

- **a directory** `agents/<n>/` with a `SOUL.md` → an **embedded** native sub-agent (`subagents.ts:114-119`, `loadNestedSubagent` at `subagents.ts:186-218`); `SOUL.md` is required, `agent.yaml` optional.
- **a `.yaml`/`.yml` file** is parsed, and **`use:` wins** (`subagents.ts:131-136`): if `ReferencedWorker.safeParse` succeeds, the file is a **WORKER** and is *never* offered as a native sub-agent, even if it also carries `description`/`prompt`.
- otherwise → inline native sub-agent (`subagents.ts:137-138`, `toInlineDef` at `subagents.ts:154-165`).
- malformed YAML is skipped, never thrown (`subagents.ts:126-130`) — a broken sibling must not break the rest.

Python already has half of this: `src/computeragent/identity/gap_subagents.py:45-63` (`load_gap_subagents`) produces only the native map and has **no `use:` branch at all**. The port adds `classify_gap_agents(workdir) -> GapAgents(native_subagents, workers)` next to it and reimplements `load_gap_subagents` as `classify_gap_agents(...).native_subagents`, exactly as `subagents.ts:150-152` does. That keeps `tests/test_gap_subagents.py` and `tests/test_gap_loader_parity.py` green unchanged.

### 2.3 The two modes

**`roster`** (`roster-run.ts:304-407`). The orchestrator LLM runs one normal turn and its *output text is the plan*. The harness parses it, fans out in code, then spends one more turn to synthesize. The orchestrator makes **no tool call** to trigger the fan-out — which is why roster mode kept working during the SRS 403 incident described at `orchestration.ts:159-166`.

**`supervised`** (`roster-run.ts:409-442`). The orchestrator runs normally with an injected in-process MCP tool `mcp__orchestration__dispatch_many` (`orchestration.ts:126-127`). When it calls the tool, workers run and the reports return **as the tool result**, into the orchestrator's context mid-turn — no withholding, no synthesis turn, no `afterEngine`. Wave events are emitted via `session.emit` directly because `DispatchHook` gets no `push` (`roster-run.ts:417-423`).

The tool is on `ORCHESTRATION_INTERNAL_TOOLS` (`orchestration.ts:189-193`) and is **short-circuited to allow without consulting the policy decider** (`run-session.ts:242`, `isOrchestrationInternalTool`). The reasoning at `orchestration.ts:142-177` is worth porting wholesale into the docstring: Cedar is default-deny, an operator writing a policy for their agent's real tools has no reason to permit a harness internal, so every supervised fan-out would be denied by a policy behaving exactly as written.

### 2.4 The roster contract

`ROSTER_CONTRACT` (`roster-contract.ts:23-33`) is appended by the **harness**, not the agent repo, to every `mode: "roster"` orchestrator's system prompt. Three clauses: emit the roster JSON and nothing else when you need workers; answer in plain prose when you do not; **never** emit an empty roster.

`appendRosterContract` (`roster-contract.ts:50-78`) handles four prompt shapes. The Python port only needs the Claude-SDK ones (there is no gitclaw engine in this SDK, so `isGitclawShaped` and the `systemPromptSuffix` branch at `roster-contract.ts:54-57` drop out):

1. `system_prompt` is a dict → extend its `append` in place, preserving the GAP `SOUL`/`RULES` text (`roster-contract.ts:62-69`);
2. `system_prompt` is a `str` → concatenate (`roster-contract.ts:71-73`);
3. absent → introduce `{"type":"preset","preset":"claude_code","append": ROSTER_CONTRACT}` (`roster-contract.ts:77`);
4. `options` is not a dict → return untouched (`roster-contract.ts:51`).

This is the same shape-handling `fork.py:172-181` (`append_steer`) already implements — that function is the direct template for `orchestration/contract.py:append_roster_contract`.

### 2.5 What `parse-roster` accepts, and the six-way `RosterOutcome`

Extraction (`parse-roster.ts:230-235`):
- a ```` ```json ```` fenced block, matched **greedily to the last** ```` ``` ```` — `/```json\s*([\s\S]*)```/`. Non-greedy truncates the JSON at the first code fence embedded inside a worker's `task` (the neuron-lyzr bug, `parse-roster.ts:86-88`). Python: `re.search(r"```json\s*([\s\S]*)```", text)` — `*` is greedy by default, so the direct transcription is correct; a reviewer must not "fix" it to `*?`.
- else a bare object matched by `/(\{[\s\S]*"(?:workers|roster)"[\s\S]*\})/`.

Keys accepted: `workers` **or** `roster` (`parse-roster.ts:241,246-248`).

Per-entry (`parse-roster.ts:137-150`): `task` must be a non-empty string or the entry is dropped; `id`, `use`, `role`, `model` are taken when strings; `tools` when an array (filtered to strings); `scope` via `scopeOf` (`parse-roster.ts:221-228`). Note what is **not** accepted: an `mcp` key in a roster entry is ignored — tool grants are author-controlled, never LLM-controlled (`parse-roster.test.ts:64`).

The six outcomes (`roster-contract.ts:52-58` declares the type; `parse-roster.ts:96-162` produces them):

| Outcome | Trigger | `isRosterText` | Scored by gate |
|---|---|---|---|
| `entries` | ≥1 usable worker | yes | yes |
| `empty-roster` | a `workers`/`roster` key holding an **empty array** | yes | **no** |
| `no-usable-entries` | array had items, none with a usable `task` | yes | yes |
| `invalid-json` | a block was found, `JSON.parse` threw | yes | yes |
| `not-a-roster` | valid JSON with neither key — most likely an illustrative fence inside prose | **no** | yes |
| `no-block` | no JSON block at all — prose | **no** | yes |

`isRosterText` (`roster-contract.ts:73-80`) is THE predicate the run flow branches on. Roster text is machinery and must never reach the user; prose IS the answer and must be left exactly as written.

**Why `empty-roster` is special.** It is the one outcome that attempted no fan-out *on purpose*: the agent emitted the roster shape and put nobody in it because the task needed no delegation. Three separate behaviours key off it:

1. **No wave events are emitted at all.** `dispatchRoster` (`roster-run.ts:145`) and `dispatchWorktreeRoster` (`worktree-dispatch.ts:55`) withhold the reporter entirely — `const waveReporter = parsed.outcome === "empty-roster" ? undefined : reporter`. Without that, the `wave_started`/`wave_ended` pair rendered a "sub-agents dispatched and merged" panel over a run that dispatched nobody (`worktree-dispatch.ts:49-54`). Every *other* zero-worker outcome keeps its pair, because those are genuine roster failures and the loudness is the point (`roster-run.ts:203-210`).
2. **The gate does not apply.** `gateApplies` (`roster-run.ts:90-92`) returns `false` only for `empty-roster`. Scoring it produced verdict `fail`, reason "no workers were dispatched" (`scorecard.ts:55-57`), on a turn where nothing was wrong.
3. **The closing turn uses the direct-answer prompt, not the wave digest** (`roster-run.ts:360-361`, `buildDirectAnswerPrompt` at `worker-digest.ts:65-78`). It still *takes* a closing turn — `isRosterText("empty-roster")` is true — because the raw `{"workers":[]}` must not reach the user.

`normalizeIds` (`parse-roster.ts:187-219`) runs only on the `entries` path. An LLM-authored `id` becomes both a worktree directory name and a git branch name, so it must be a single safe path segment (`SAFE_SEGMENT = /^[A-Za-z0-9._-]{1,128}$/`, `path-jail.ts:47`). The id is a *label*, not a lookup key (`use` names the crew entry; nothing joins on `id`), so the right move is slugify-not-fail: reserve every positional `w<n>` fallback first (`parse-roster.ts:191-192`), slugify, drop the id entirely if nothing survives, de-duplicate with a `-2` suffix trimmed to 120 chars first, and log every rewrite.

---

## 3. Worker dispatch and isolation

### 3.1 How TS spawns a worker

A worker is a **sibling `Session` in the same `SessionRegistry`, in the same process and the same event loop** — `spawn-child.ts:17-27`. It is created by the ordinary `createSession` + `runSession` machinery, not by anything worker-specific. Two dispatch sites:

- **`isolation: "none"`** — `spawnChildSession` (`spawn-child.ts:97-206`). The worker's identity is a *different repo* resolved from the crew entry's `use:` (`crew.ts:43-71`, `useToSource`), cloned by the substrate.
- **`isolation: "worktree"`** — `dispatchWorktreeEntries` (`worktree-dispatch.ts:73-292`). The worker is defined **inline by the roster** (`role`, `scope`, `model`, `tools`) and runs on a synthetic 2-line GAP identity (`worktree-dispatch.ts:313-319`) inside its own git worktree of the *orchestrator's* repo.

### 3.2 What a worker inherits

| Inherited | Where | Note |
|---|---|---|
| **Engine** | `spec.engine ?? parent.engineName` (`spawn-child.ts:120`); worktree path always `orchestrator.engineName` (`worktree-dispatch.ts:165`) | |
| **Identity loader** | `spec.loader ?? parent.loaderName` (`spawn-child.ts:148`) | |
| **Model** | roster entry's, **else the orchestrator's resolved model** (`worktree-dispatch.ts:344`, `parentModelOf` at `worktree-dispatch.ts:365-371`) | The fallback is load-bearing: without it the CLI substituted its own default, which a gateway-routed deployment rejects outright (`worktree-dispatch.ts:336-343`) |
| **Envs** | `{...parent.envs, ...spec.envs}` (`spawn-child.ts:151`); worktree: `{...orchestrator.envs}` (`worktree-dispatch.ts:180`) | |
| **MCP servers** | `inheritMcpInto` (`worker-mcp.ts:110-195`) per the resolved policy | See §3.5 |
| **The parent's `PolicyDecider` INSTANCE** | `inheritPolicyFrom: orchestrator.policyDecider` (`spawn-child.ts:165`, `worktree-dispatch.ts:196-198`) → `create-session.ts:182-192` | One decider object shared across the wave ⇒ **one SRS policy fetch per wave**, not per worker. Without it, "the orchestrator's every tool call is gated while its workers' Bash/Write/MCP calls run through nothing" (`spawn-child.ts:162-164`) |
| **Guardrail identity** | `policyAgentName: orchestrator.policyAgentName` (`spawn-child.ts:114`, `worktree-dispatch.ts:189-191`) | The worker presents the *orchestrator's* identity to the guardrail, matching the inherited decider's principalId |
| **Scope guard** | `scopeWrite: entry.scope.write` when non-empty (`spawn-child.ts:159-161`, `worktree-dispatch.ts:202-204`) → `create-session.ts:262-265`, **`unshift`ed** ahead of the network decider so the composite's first-deny short-circuit makes an out-of-scope write cost zero SRS calls | |
| **Budget cap** | `maxBudgetUsd` = the fair-share slice, min'd with any cap the crew already declared (`roster-run.ts:236-239`; worktree: `worktree-dispatch.ts:360`) | |
| **`unattended: true`** | `spawn-child.ts:158`, `worktree-dispatch.ts:201` | Nobody is subscribed to a worker's stream, so a permission request it raises can never be answered (`scope-guard.ts:94-104`). In the Claude-SDK case this is covered by `permissionMode: "bypassPermissions"` (`worktree-dispatch.ts:354`) |
| **Parent linkage** | `parentSessionId` / `parentAgentId` (`spawn-child.ts:106-110`) | |

### 3.3 Recommendation for Python: in-process sibling coordinators

**Use in-process siblings, matching TS. Do not use child `ComputerAgent` processes.**

Concretely, a worker is a second `HarnessCoordinator` constructed directly (not via `ComputerAgent`), in the parent's event loop, sharing:

- the parent's `Pipeline` object (`telemetry_pipeline=self._pipeline`) — so worker telemetry lands on the same sinks with no re-configuration;
- the parent's `PolicyDecider` callable (`policy_decider=self._policy_decider`, possibly composed with a fresh scope guard — §3.6) — **one SRS fetch per wave**;
- the parent's `envs` dict;
- `parent_session_id=self._lifecycle_session_id`, `parent_agent_id=self._agent_id` — the stamping at `coordinator.py:596-600` then does the rest for free.

Reasons, beyond the five in §1.1:

1. **`ClaudeAgentEngine` already spawns a process per session anyway.** `claude_agent_sdk.query()` (`engines/claude_agent.py:63`) launches the Claude Code CLI as a subprocess. So "in-process" is a statement about the *harness* objects, not about isolation of the model loop — we get the same process-level isolation of the agent runtime either way, and pay for it once. A child `ComputerAgent` would spawn a CLI **and** duplicate the harness, the substrate boot, the identity load, and the AgentOS agent resolve.
2. **Concurrency is a plain `asyncio` pool.** `max_parallel` becomes `min(max_parallel, len(entries))` coroutines each pulling from a shared index, exactly `worktree-dispatch.ts:151-268` / `spawn-child.ts:225-248`. With child processes the pool would have to coordinate through a semaphore inside an MCP tool handler the model drives (`fork.py:359`), which is not the same guarantee.
3. **Cancellation is one `Task.cancel()`.** `ChatHandle.cancel()` (`harness/__init__.py:139-145`) sets `state.cancel_event` and pushes the `None` sentinel. Extending that to cancel a wave task is three lines. Killing N detached child processes reliably is not (`fork.py:164-169` is best-effort and `fork.py:394-398` relies on `asyncio.wait_for` cancellation propagating through a whole `ComputerAgent.dispose`).
4. **The worktree path *requires* it.** A worker's workspace is `tree.path`, a directory the parent created seconds earlier (`worktree-dispatch.ts:159-165`). Handing that to a child process means the child's `LocalSubstrate.boot` must be told "this Path, don't clone, don't delete" — which it does support (`substrates/local.py:48-49`) — but the child would then also run the full identity loader against it, which is precisely the write that `restoreBaselinePaths` exists to undo (§3.4). Doing it in-process lets us skip the loader entirely for inline workers and construct the `AgentManifest` directly.

**The one thing the separate-process model had that we lose:** a genuinely independent failure domain. A worker that hard-crashes the Python process takes the orchestrator with it. Mitigations: every worker coroutine is wrapped so any exception becomes a `WorkerResult(status="errored")` (`spawn-child.ts:74-75,168-170,252-261`; `worktree-dispatch.ts:244-265`), and the whole wave is wrapped in `asyncio.wait_for` (§5.4). That matches TS, which has the same exposure.

### 3.4 Worktree isolation — the exact git sequence

Ported from `worktree.ts`. All git invocations force identity on the command line so no ambient config is needed (`worktree.ts:49-69`):

```
git -c user.email=orchestrator@example.invalid -c user.name=Orchestrator -c commit.gpgsign=false -C <cwd> <args>
```

**`init_baseline(repo) -> base_sha`** (`worktree.ts:89-101`):
1. if `<repo>/.git` does not exist → `git init -b orchestration-baseline` (`worktree.ts:91`). Always explicit, **never via discovery**, which could resolve to a parent repo (`worktree.ts:86-88`).
2. write `<repo>/.git/info/exclude` with the `EXCLUDES` list (`worktree.ts:93-95`).
3. `git add -A`
4. `git commit --allow-empty -m "baseline before orchestration"` — `--allow-empty` so a genuinely empty workspace still yields a base commit (`worktree.ts:98-99`).
5. return `git rev-parse HEAD`.

`EXCLUDES` (`worktree.ts:24-36`) — **10 entries, and the last two matter most**:
```
__pycache__/   *.py[cod]   .pytest_cache/   .ruff_cache/   .mypy_cache/
*.egg-info/    .coverage    .DS_Store        node_modules/
agent.yaml     .claude/
```
The first eight are build/cache droppings a worker creates merely by running. `agent.yaml` and `.claude/` are the worker's own materialized GAP identity files, excluded so they never count as artifacts (`worktree.ts:21-23`).

**`create_worktree(repo, worker_id, base_sha)`** (`worktree.ts:103-131`):
1. `assert_safe_path_segment(worker_id)` — the id is LLM-authored and becomes both a directory and a branch name (`worktree.ts:104-107`).
2. worktrees root = `join(dirname(repo), basename(repo) + ".worktrees")` — **BESIDE the repo, never inside it**, else they would pollute its own diff (`worktree.ts:80-83`).
3. `rm -rf <root>/<worker_id>`
4. `git worktree prune` (non-checking) — drops registrations whose directory we just deleted or that a crashed run left behind (`worktree.ts:113-116`).
5. `git worktree add --force -B orchestration/<worker_id> <path> <base_sha>`.
   - **`-B`, not `-b`** (`worktree.ts:117-128`): a second fan-out in the same session reuses worker ids (`w1`, `w2` — the orchestrator's own prompt prescribes them) and `cleanup()` removes worktrees but never branches, so `-b` failed with "a branch named 'orchestration/w1' already exists" and the worker came back `errored` with 0 artifacts. Resetting is also the semantics we want independently: a wave's worker starts from the pristine baseline, never from whatever the last wave left.
   - **`--force`** waives the "already checked out elsewhere" guard for the case where cleanup did not run.

**`restore_baseline_paths(tree, ["agent.yaml"])`** (`worktree.ts:217-230`), called **after the worker's session is created but BEFORE the worker runs** (`worktree-dispatch.ts:216-220`). For each path: if `git cat-file -e <base_sha>:<rel>` succeeds → `git checkout <base_sha> -- <rel>`; else `rm -f` it (the loader *created* it, so the right undo is deletion). Best-effort; a failure here must not lose the worker.

The failure this prevents is catastrophic and is documented at `worktree.ts:195-216`: the inline identity is materialized *into* the worktree, overwriting the orchestrator repo's `agent.yaml` with a synthetic 2-line stub. Every worker's branch then modified `agent.yaml` with *different* content (`name: w1` vs `name: w2`), so **any wave with ≥2 workers conflicted on it no matter what the task was**; `git merge --abort` is atomic, so the second worker's entire diff — including its only real deliverable — was discarded; the integrated tree ended up with the agent's manifest replaced by the stub (observed: 48 lines → 2); and the scope guard could not prevent any of it, because it gates `Write`/`Edit` **tool** calls while this write comes from the loader.

> **Python note:** this whole hazard is avoidable in the port, because §3.3 lets us skip the identity loader for inline workers entirely and synthesize the `AgentManifest` in memory. **Implement `restore_baseline_paths` anyway.** It is cheap, it is the belt to the in-memory braces, and the moment anyone wires a real `use:` worker into a worktree (or GAP skill mirroring starts writing `.claude/`) the hazard is back. `INLINE_IDENTITY_PATHS` (`worktree-dispatch.ts:311`) stays a single-element tuple `("agent.yaml",)` and must be kept in sync with whatever the synthetic identity writes.

**`diff_paths(tree)`** (`worktree.ts:134-142`): `git add -A` (staging first makes untracked files visible), then `git diff --cached --name-only <base_sha>`, split/strip/filter/sort.

**Taken BEFORE the merge.** `worktree-dispatch.ts:225-233`: `collectSession` → `diffPaths(tree)` → `commitWorker(tree, ...)`. This ordering **is the artifact attribution**: worker A's writes can never appear in worker B's diff under parallel dispatch, because each diff is against the shared baseline in a separate working tree (`worktree.ts:7-16`). After the merge there is only a wave-level total the client would have to attribute itself (`worktree-dispatch.ts:231-233`).

**`commit_worker(tree, msg)`** (`worktree.ts:145-152`): `git add -A`, `git status --porcelain`; empty → return `False` (worker changed nothing, and its tree is skipped by `integrate`). Else `git commit -m`, set `tree.committed = True`.

**`integrate(repo, trees, base_sha)`** (`worktree.ts:159-185`):
1. `git checkout -B orchestration-integration <base_sha>`
2. for each **committed** tree: `git merge --no-edit --no-ff <branch>`.
   - `--no-ff` so every worker's contribution is a distinguishable merge commit.
   - on failure: capture `git diff --name-only --diff-filter=U` (unmerged paths) as `conflicts[worker_id]`, falling back to `["<merge failed with no unmerged paths>"]` if empty, then **`git merge --abort`** — never force-resolve. "The conflict itself is the finding worth reporting" (`worktree.ts:154-158`); two workers whose scopes did not overlap *cannot* conflict, so a conflict is direct evidence of an overlap.
3. `integrated` = `git diff --name-only <base_sha>`, sorted.

**`cleanup(repo, trees)`** (`worktree.ts:188-193`): `git worktree remove --force <path>` per tree (non-checking), then `rm -rf` the worktrees root. Best-effort — never fail a run over cleanup. Note it removes worktrees but **not** branches; that is why `-B` above is required.

**Python implementation notes for `orchestration/worktree.py`:**
- Use `asyncio.create_subprocess_exec("git", *args, stdout=PIPE, stderr=PIPE)` + `await proc.communicate()`. Do **not** shell out through a string.
- TS caps stdout at 32 MB (`worktree.ts:62`); a repo with a very wide diff can exceed the default `asyncio` stream limit (64 KiB) and raise `LimitOverrunError`. Pass `limit=32 * 1024 * 1024` to `create_subprocess_exec`.
- `git` must be on `PATH`. Add an explicit `shutil.which("git")` preflight in `init_baseline` that raises a clear `OrchestrationError` rather than letting `FileNotFoundError` surface from the first `create_subprocess_exec`.
- On macOS `dirname(repo)` is frequently `/var/folders/...` under `TMPDIR`; `.worktrees` siblings there are fine but `tmp_path` in pytest is a better fixture root (§9).

### 3.5 MCP inheritance

`resolve_worker_mcp(entry_mcp, spec_mcp) -> entry ?? spec ?? "inherit"` (`worker-mcp.ts:33-38`). Resolved at the one place that sees **both** the crew entry and the orchestration block (`roster-run.ts:183`); the worktree path resolves with `undefined` for the entry because a roster entry may not grant itself tools (`worktree-dispatch.ts:168-176`).

`inherit_mcp_into(child_options, parent_options, policy, engine_name)` (`worker-mcp.ts:110-195`):
1. `policy == "none"` → return `child_options` unchanged (same object).
2. no `mcpServers` on the parent → unchanged.
3. names = all parent keys for `"inherit"`, else the explicit list. An unknown name is **skipped with a warning**, not fatal — a stale name in a crew file costs that one tool, not the wave (`worker-mcp.ts:125-134`).
4. **Skip in-process SDK servers** (`type === "sdk"`, `worker-mcp.ts:64-66`). The entry holds a live instance served over the SDK control protocol by the session that created it; a second session presenting the same object reports `{name:"gap_tools", status:"failed"}`, and worse, the inherited entry *wins* over the worker's own, replacing a `gap_tools` that would have worked with one that cannot (`worker-mcp.ts:48-63`).
5. merge: inherited entry **wins** over a same-keyed repo-declared one, because it carries the live dispatch-time credential (`worker-mcp.ts:148-156`).
6. **Only EXTEND an existing `allowedTools`; never create one.** Creating one would flip "every tool allowed" into "only these MCP tools allowed" and strip the worker of Read/Write/Bash (`worker-mcp.ts:158-173`). Extension happens for *every* engine, not just Anthropic-shaped, because a worker's allowlist comes from its roster entry's `tools` — authored by the orchestrator's model — and leaving it alone let a model-written list silently revoke a grant the wave's configuration made (measured: two workers reported `NO_DEEPWIKI_TOOL`, made zero bridge calls, and still came back `completed` under a `pass` scorecard).
7. Grant form per engine (`worker-mcp.ts:84-87`): Anthropic-shaped → one server-level entry `mcp__<server>`; gitclaw → every tool named individually from the bridge URL's `?tools=` query.

**Python simplifications:** there is exactly one engine (`claude-agent-sdk`), so `ANTHROPIC_SHAPED` collapses to a constant `True` and `boundToolNamesOf`/`grantsFor`'s gitclaw branch (`worker-mcp.ts:89-98`) drops out — keep a comment saying why. Option keys are snake_case: `mcp_servers`, `allowed_tools`. The in-process check is not `type === "sdk"` on a dict but an `isinstance` / duck-type check against whatever `claude_agent_sdk.create_sdk_mcp_server` returns — the safest form is `not isinstance(entry, dict)` **or** `entry.get("type") == "sdk"`, covering both the object form the Python SDK returns and the dict form a caller might pass. Port the `worker-mcp.ts` warning log lines verbatim (`worker_mcp.unknown_server`, `worker_mcp.skipped_in_process_server`, `worker_mcp.inherited_by_referenced_worker` at `spawn-child.ts:133-138`) — that last one is the line to grep when auditing who got which credential.

### 3.6 Scope guard — and why `fnmatch` will not do

`ScopeGuardDecider` (`scope-guard.ts:50-87`) is a `PolicyDecider` that denies writes outside `scope.write`:
- non-write tools → allow unconditionally (`scope-guard.ts:62`). `WRITE_TOOLS` is lowercased: `{write, edit, multiedit, notebookedit}` (`scope-guard.ts:32`). The case-insensitive match is not cosmetic — an exact-case match made `has("write")` false and returned ALLOW for every gitagent write (`scope-guard.ts:20-31`).
- path fields tried in order: `file_path`, `notebook_path`, `path`, `filePath` (`scope-guard.ts:34`).
- a write tool with **no recognizable target** → allow, measured post-hoc against the worktree diff. "Guessing would be worse than measuring" (`scope-guard.ts:65-67`).
- shell tools (`Bash`) are deliberately unguarded — a command string does not say what it touches (`scope-guard.ts:14-17`).
- resolve relative to the workspace; escaping it → deny (`scope-guard.ts:124-131`).
- empty `scopeWrite` → deny every write (`scope-guard.ts:76-78`).
- `alwaysAllow` exempts a path regardless of scope (`scope-guard.ts:75`).

`AllowAllDecider` (`scope-guard.ts:106-110`) exists so an unattended worker never blocks on an unanswerable permission request. In the Python port it is only reachable if we ever bind a decider under a non-bypass permission mode; keep it, with the `scope-guard.ts:90-105` docstring, and note that `claude-agent-sdk` under `bypassPermissions` skips `can_use_tool` entirely.

**The glob problem.** `matchesAny` uses `minimatch(rel, glob, {dot: true})` (`scope-guard.ts:133-135`). Python's `fnmatch` is **not equivalent**, in two ways that both fail *open*:

| Pattern | Path | minimatch `{dot:true}` | `fnmatch.fnmatch` |
|---|---|---|---|
| `src/*.py` | `src/a/b.py` | **no** (`*` does not cross `/`) | **yes** — wrong, widens scope |
| `src/**/*.py` | `src/a/b.py` | **yes** | yes (by accident) |
| `src/**` | `src/.hidden` | **yes** (`dot:true`) | yes |
| `*.py` | `src/a.py` | **no** | **yes** — wrong |

`fnmatch`'s `*` matching `/` means a worker scoped to `src/*.py` could write `src/anything/anywhere.py`, and a worker scoped to `*.md` could write the entire tree. That is a security-relevant difference, not a cosmetic one, because roster `scope.write` globs are **LLM-authored** (`parse-roster.ts:221-228`).

`pathlib.PurePath.match` is also wrong: it matches from the right (`PurePath("src/a/b.py").match("*.py")` is `True`) and `**` support only arrived in 3.13, while this package targets `>=3.10` (`pyproject.toml`).

**Recommendation: `wcmatch`.** `wcmatch.glob.globmatch(rel, pattern, flags=GLOBSTAR | DOTGLOB)` is the closest published equivalent to minimatch's `**` + `{dot:true}`. Add it as a new optional extra:

```toml
orchestration = ["wcmatch>=8.5,<11"]
```

`pathspec` is the alternative but is a *worse* match: it implements gitignore semantics, where a bare `src` matches the directory recursively and a leading `/` anchors — different rules, silently. If the dependency is refused, the fallback is a hand-written matcher that translates a glob to a regex with `**` → `.*`, `*` → `[^/]*`, `?` → `[^/]`, and no implicit dot-hiding; budget half a day and a dedicated test table, and expect drift on brace expansion (`{a,b}`) which minimatch supports and this would not.

**Python wiring differences from TS.** The Python `PolicyDecider` protocol (`src/computeragent/protocol_types/__init__.py:262-275`) is `async def __call__(tool_name, tool_input, context) -> Any`, returning a `claude_agent_sdk` `PermissionResultAllow`/`PermissionResultDeny` — *not* TS's `evaluate(ctx) -> PolicyDecision`. So:
- `ScopeGuardDecider.__call__` returns `PermissionResultDeny(message=...)` / `PermissionResultAllow(updated_input=tool_input)`.
- There is **no `CompositePolicyDecider` in this SDK.** One must be written (`orchestration/scope_guard.py: CompositeDecider`): deny-if-any, first-deny short-circuits, scope guard **first** because it is a local glob match while the inherited SRS decider is a network round-trip (`create-session.ts:257-261`).
- **Critically:** workers run `permission_mode="bypassPermissions"` (`worktree-dispatch.ts:354`), and under that mode the Claude CLI **skips `can_use_tool` entirely**. `engines/claude_agent.py:107-130` already handles this — it registers a `PreToolUse` hook for `bypassPermissions`/`dontAsk`/`auto`/`acceptEdits` that routes the same bound decider, merged with (never replacing) GAP/caller hooks. The worker coordinator must therefore pass its composed decider as `policy_decider` on `EngineContext` and let `_build_options` do the rest. Ship a test that asserts a scope-guard denial actually fires under `bypassPermissions`; without the hook the guard is dead code.

### 3.7 Crew `use:` resolution

`useToSource` (`crew.ts:43-71`): `/abs`, `./rel`, `../rel`, `~/…` → local path; `https://host/org/repo[@ref]` → git with the scheme kept; bare `host/org/repo[@ref]` → git with `https://` prepended. The `@ref` split only fires when a `/` precedes the `@`, so `git@host:...` is not mistaken for a ref (`crew.ts:55-63`). A bare registered-agent id is **not** resolvable — treated as a git URL best-effort and surfaced as a worker error (`crew.ts:38-41`).

Python's `IdentitySource` is `Path | str | dict` (`protocol_types/__init__.py:21-32`), so `use_to_source` returns a `Path` for the local forms and a `str` URL for the git forms; `LocalSubstrate.boot` already branches on exactly that (`substrates/local.py:48-57`). **Open question (§10.3):** in single-run mode a bare agent id should plausibly resolve through `GET {base}/v1/agents/{id}` rather than failing as a clone — that is TS's deferred "Phase 6".

---

## 4. Budget and scorecard

### 4.1 `BudgetAllocator` (`budget.ts:43-103`)

- `MIN_VIABLE_USD = 0.05` (`budget.ts:19`) — "below this there isn't enough left to be worth starting anything".
- `perWorkerUsd` default `4.0` (`budget.ts:48`).
- **Fair-share formula:** `slice(n) = min(perWorkerUsd, remaining() / max(1, n))` (`budget.ts:64-67`). The `/n` is the whole point: workers run **concurrently**, so giving N in-flight workers the whole remaining budget each lets the stage overrun by N× while every individual worker stays inside its own cap (`budget.ts:4-9`).
- `remaining() = max(0, totalUsd - spent)`; `spent` = sum of settlement `usd` (`budget.ts:51-57`).
- `exhausted() = remaining() < MIN_VIABLE_USD` (`budget.ts:59-61`).
- `checkBeforeDispatch(n)` (`budget.ts:70-79`) returns the cap or raises `BudgetExceeded` with the exact message `"$X left of $Y gives N worker(s) $Z each, below the $0.05 minimum. Aborting before dispatch."` (2dp / 2dp / 3dp / 2dp).
- `record(workerId, usd, source)` clamps negatives to 0 (`budget.ts:82-84`).
- **Settlement sources** (`budget.ts:16`): `"sdk"` (a measured cost the engine reported), `"estimated"` (a guess), `"stub"` (declared but not produced by either dispatch site). Both dispatch sites choose with the same expression: `source = "sdk" if outcome.usage.costUsd is not None else "estimated"` (`roster-run.ts:262`, `worktree-dispatch.ts:228`).
- `estimatedShare` (`budget.ts:87-91`) = (sum of non-`sdk` settlements) / `spent`, `0` when `spent <= 0`. It is the honesty signal: a wave whose accounting is mostly guesses says so.
- `summary()` rounds to 4 dp, except `estimatedShare` to 3 (`budget.ts:93-102`).
- `warnUnenforceableBudget` (`budget.ts:119-133`) logs when the engine declares `capabilities.budget === false`, because `maxBudgetUsd` is then ignored and only `checkBeforeDispatch` bounds the wave. **Python: `ClaudeAgentEngine.capabilities.budget` is `True` (`engines/claude_agent.py:34`)**, so this warns for nothing today — port it anyway as a one-line guard, since the check is on `EngineCapabilities` and a future engine flips it.

**Two layers, stated plainly:** refuse an under-funded wave *before* dispatch (a clean, cheap abort), and enforce the per-worker cap *in-session* via `maxBudgetUsd`. In Python the second layer maps to `HarnessCoordinator(budget_max_usd=...)` → `EngineContext.budget_max_usd` (`protocol_types/__init__.py:143`).

`Math.min((s.options?.maxBudgetUsd) ?? Infinity, cap)` (`roster-run.ts:238`) — a tighter cap already declared by the crew still wins. In Python use `min(existing if existing is not None else math.inf, cap)`.

### 4.2 Scorecard and the four gate-failure reasons

`evaluateGate(input)` (`scorecard.ts:46-70`) accumulates `reasons`; verdict is `"pass"` iff `reasons` is empty.

1. **`aborted` set** → `` `budget aborted the wave: ${aborted}` `` (`scorecard.ts:49`).
2. **zero workers and not aborted** → `"no workers were dispatched — the roster was empty or did not parse"` (`scorecard.ts:55-57`). Without this the gate certified every silent roster failure as green.
3. **per worker, `status !== "completed"` or `error` set** → `` `worker ${id} did not complete cleanly: ${error ?? status}` `` (`scorecard.ts:59-63`).
4. **per conflict entry** → `` `worker ${id} conflicts on ${paths.join(", ")} (scope overlap)` `` (`scorecard.ts:65-67`).

`buildScorecard(agent, input)` (`scorecard.ts:77-94`) wraps it with per-worker rows (`id`, `status`, `artifacts` **count**, optional `costUsd`, optional `error`), `integrated`, `conflicts`, optional `budget`, and `status` mirroring `gate.verdict`.

The gate is only computed when `collect === "gate"` **and** `gateApplies(outcome)` (`roster-run.ts:334`). Under `collect: "into-context"` no scorecard is computed at all and its absence is not a failure (`sse-events.ts:342-345`).

---

## 5. The turn lifecycle — the subtle part

### 5.1 What TS does

`runSession` (`run-session.ts:69-…`) drives the engine and owns four pieces of state (`run-session.ts:128-147`): `afterEnginePending`, `turnRole: "primary" | "synthesis"`, `followUpPrompt`, and `withheldResult`.

On each `sdk_message`, `extractMessageText` taps assistant text and the terminal `result` (`run-session.ts:393-406`). **A `result` payload IS the turn boundary** (`run-session.ts:407-409`) — not the end of the engine loop, because the SDK always opens with `streamingInput: true` and only sends `/end-input` from `dispose()`, so the loop does **not** close at a turn boundary. An end-of-loop-only hook therefore never ran on any CAS path (`run-session.ts:112-127`).

On `sawResult && turnRole === "primary"` (`run-session.ts:430-476`):
1. `await flushAfterEngine()` — the hook runs the whole wave, with `push` writing into the session channel *before* the terminal event (`run-session.ts:179-199`).
2. if the engine reported `is_error`, the follow-up is declined — a broken path must not be retried and its error terminator must not be replaced (`run-session.ts:432-443`).
3. if the hook called `requestFollowUp(prompt)`: set `turnRole = "synthesis"`, stash `withheldResult = event.payload`, arm a `setTimeout` watchdog (default `300_000` ms, `run-session.ts:49,461-472`), `session.enqueueInternalMessage({role:"user", content: prompt})`, and `continue` — **the primary result is never pushed.**
4. On the synthesis turn's own result (`run-session.ts:420-429`): clear the timer, drop `withheldResult`, reset buffers, `turnRole = "primary"`, and fall through so *that* result is pushed as the turn's terminator. The follow-up turn deliberately does **not** re-enter `afterEngine` — handing the closing message to the hook would parse the user-facing answer as another roster.
5. After the loop: `releaseWithheldResult("engine stream ended before the follow-up turn ran")` (`run-session.ts:519-522`) — deterministic recovery for an engine whose stream simply ends at the turn boundary.

Why withhold at all, in TS: "the client SDK synthesizes its own `ca_session_ended` the moment it sees this payload, and CAS then ends the turn and closes the sandbox chat — which on a remote substrate kills the guest harness process and every worker still running inside it. Observed exactly that: the workers spawned, and one second later the turn was torn down mid-flight" (`run-session.ts:411-419`).

### 5.2 Why Python has the same problem for different reasons

There is no remote sandbox here, so the teardown argument does not transfer. Three others do, and they are enough:

1. **Telemetry.** `_stream_engine_events` tracks `result_payload = _maybe_result_payload(...)` (`coordinator.py:452`, helper at `coordinator.py:770-780`) and, on clean completion, assigns it to `self._lifecycle_last_result_payload` (`coordinator.py:527-528`), which becomes the `result` field of the single `session_ended` AgentOS row (`coordinator.py:279-286`). Left alone, **the recorded final answer of every orchestrator run is the raw roster JSON.** Same for `_lifecycle_last_assistant_text` (`coordinator.py:525-526`).
2. **Consumers key on `ResultMessage`.** The drop-in contract of this SDK is that a caller iterates until a `ResultMessage` — the README examples, `latest.py`, and `_proxy/query.py` all do. A roster `ResultMessage` reaching the stream ends the turn for every such caller, exactly as in TS.
3. **`ChatResult`.** `ChatHandle._finalize_result` (`harness/__init__.py:147-156`) snapshots `state.messages`. A consumer doing `result = await agent.chat(...)` and reading the last message would get the roster.

### 5.3 The precise Python mechanism

Four edits, all in `src/computeragent/harness/coordinator.py`.

**(a) Hold the sentinel back — `_enqueue_user_message` (`coordinator.py:712-741`).**

The function unconditionally does `await queue.put(None)` at `coordinator.py:741` ("Single-shot mode — close the queue after one ChatInput is drained"). `_QueueAsyncIterable._iter` (`coordinator.py:687-693`) returns on `None`, ending `ClaudeAgentEngine._wrapped_prompts` (`engines/claude_agent.py:52-56`), which ends the CLI session after one turn.

Add a keyword-only parameter and pass it from the one call site:

```python
async def _enqueue_user_message(queue, message, *, close: bool = True) -> None:
    ...
    if close:
        await queue.put(None)
```

At `coordinator.py:348`: `await _enqueue_user_message(state.user_queue, message, close=not self._orchestration_active)`.

The recursive `AsyncIterable` branch (`coordinator.py:733-734`) already recurses into itself and would double-close today; it must pass `close=False` on the inner call regardless — a latent bug worth fixing in the same edit.

`_orchestration_active` is set in `_ensure_booted` as `self._orchestration is not None and self._orchestration.mode == "roster"`. **Supervised mode keeps `close=True`** — its workers run inside a tool call, so the queue can close normally.

**(b) Withhold the terminal result — `_stream_engine_events` (`coordinator.py:412-540`).**

Inside `elif ev.kind == "sdk_message":` (`coordinator.py:447`), after the existing `result_payload = _maybe_result_payload(...)` line (`coordinator.py:452`), insert the turn-boundary branch. Sketch (not final code):

```python
saw_result = _maybe_result_payload(ev.payload, None) is not None
if saw_result and self._orchestration_active and turn_role == "primary":
    # telemetry still sees it; the consumer does not
    state.messages.append(ev.payload)          # already done above
    await self._emit_upstream_telemetry(ev)    # existing from_message path
    if _is_error_result(ev.payload):           # run-session.ts:436
        turn_role = "closed"
    else:
        withheld = ev.payload
        prompt = await self._run_wave(state, ev)   # §5.4 — wave + digest
        if prompt is None:
            turn_role = "closed"                   # nothing to synthesize
        else:
            turn_role = "synthesis"
            await state.user_queue.put({"role": "user", "content": prompt})
            self._arm_follow_up_watchdog(state)
    if turn_role != "synthesis":
        yield ev                                   # release the withheld result
    continue                                       # do NOT yield it otherwise
if saw_result and turn_role == "synthesis":
    withheld = None
    turn_role = "primary"
    self._cancel_follow_up_watchdog()
    await state.user_queue.put(None)               # close for real
    # fall through: this result IS the turn terminator
```

Points that matter:

- The existing telemetry translation (`coordinator.py:453-498`) must run for the withheld message **before** the `continue`, so usage and message history stay correct. Restructure the block so the emit happens first and the `yield ev` is what is conditional — do not duplicate the translation.
- `ClaudeAgentEngine` yields a **separate** `usage_snapshot` `EngineEvent` immediately after each `ResultMessage` (`engines/claude_agent.py:69-75`). That one is **not** withheld: it is not a turn terminator for any consumer, and passing it through is what preserves cost accounting across the withhold, matching `run-session.ts:457-459`.
- `withheld` must be released on **every** exit path. The generator's `except BaseException` (`coordinator.py:514`) and a new `finally` both need `if withheld is not None: yield withheld` — except a bare `yield` inside `finally` during `GeneratorExit` raises `RuntimeError`. The safe form is to release in the `else:` clause (`coordinator.py:520`) and in an explicit timeout path, and to *drop* it silently on cancellation (where the consumer is gone anyway).

**(c) The wave runs as a task, awaited by the generator.**

`self._run_wave(...)` must not simply `await` the dispatcher inline. Store it:

```python
state.wave_task = asyncio.create_task(run_wave(...))
try:
    result = await asyncio.wait_for(asyncio.shield(state.wave_task), timeout=self._wave_timeout_s)
except asyncio.TimeoutError:
    state.wave_task.cancel()
    return None          # release the withheld result, no synthesis
```

Why a task rather than a bare `await`: the wave is being driven by the consumer's `async for`. A consumer that `break`s out mid-wave throws `GeneratorExit` into the generator at its next suspension point; with a bare `await` the wave dies half-integrated, leaving worktrees and branches behind. As a task it survives long enough for a `finally` to cancel it deliberately and call `cleanup(repo, trees)`. `ChatHandle.cancel()` (`harness/__init__.py:139-145`) and `HarnessCoordinator.dispose` (`coordinator.py:243`) both gain `if state.wave_task: state.wave_task.cancel()`, replacing the `_fork_context.teardown()` call at `coordinator.py:250-252`.

Document in `ChatHandle`'s docstring: **under orchestration, `await handle` (full drain) is the supported form; breaking out of `async for` early aborts the wave.**

**(d) Two watchdogs.**

1. **Wave watchdog** — the `asyncio.wait_for` above. Default `wave_timeout_s = 900.0` (15 min), configurable. On expiry: cancel the wave task, log `orchestration.wave_timeout`, emit `ca_worker_wave_ended` with `aborted="wave timed out after Ns"`, release the withheld result, close the queue. The turn terminates even with workers wedged. This is net-new vs TS, which has no wave-level timeout (only the per-follow-up one) — worth having, because a wedged `git merge` prompt or a hung CLI would otherwise hang the caller forever.
2. **Follow-up watchdog** — TS's `DEFAULT_FOLLOW_UP_TIMEOUT_MS = 300_000` (`run-session.ts:49`). A `asyncio.create_task` that sleeps `follow_up_timeout_s` (default 300.0) and, if `turn_role` is still `"synthesis"`, logs `session.follow_up_unanswered`, pushes `None` onto the user queue (ending the engine loop) and sets a flag so the `else:` clause releases the withheld result. `.cancel()` it the moment the synthesis result arrives — a stray watchdog pushing after the terminal event is the exact hazard `run-session.ts:536-538` guards.
3. **Deterministic non-timeout recovery**, mirroring `run-session.ts:519-522`: if the engine stream simply **ends** while `turn_role == "synthesis"`, the generator's `else:` clause releases the withheld result immediately rather than making the caller wait out the 300 s. This is the common failure, not the timeout.

**Kill switch.** TS reads `ORCHESTRATOR_SYNTHESIS` (`roster-run.ts:31-34`): anything but `"0"`/`"false"` enables synthesis. Port the same env var name so operators' runbooks transfer.

### 5.4 The decision table the wave runner applies

Three predicates from `roster-run.ts`, ported to `orchestration/runner.py`:

- **`wave_needs_synthesis(r, outcome)`** (`roster-run.ts:62-72`): `True` if any worker ran, or `aborted` is set, or `unmatched` is non-empty; otherwise `is_roster_text(outcome)`. A `roster` agent is not obliged to fan out — when it answered in prose, its own text is already the final answer, and synthesizing would spend an LLM call *and* replace that answer with a summary of nothing (`roster-run.ts:36-60`).
- **`no_reports_to_fold(r)`** (`roster-run.ts:100-106`): nothing ran, nothing aborted, nothing unmatched — so the turn exists solely because the orchestrator's text was roster JSON, and it needs `build_direct_answer_prompt` rather than a wave digest (`worker-digest.ts:65-78`).
- **`direct_answer_reason(outcome)`** (`roster-run.ts:109-111`): `"empty-roster"` for that outcome, else `"unparsed-roster"`.

The digest itself (`worker-digest.ts:88-148`): per-worker cap `PER_WORKER_CHARS = 8_000`, total cap `TOTAL_CHARS = 60_000` applied after per-worker truncation (`worker-digest.ts:23-25`), truncation marker `"\n…[truncated N chars]"`. The closing instruction block (`worker-digest.ts:140-145`) is load-bearing: the orchestrator's own system prompt told it to answer with a roster, so without an explicit "do NOT emit another roster" the obvious completion is another one.

---

## 6. Events

### 6.1 The five events and their exact ordering

Guaranteed ordering (`sse-events.ts:197-204`, i.e. the header comment at lines ~197-204 of `packages/protocol/src/sse-events.ts`):

```
ca_session_started … orchestrator sdk_messages …
  ca_worker_wave_started
    ca_worker_started / ca_worker_completed   (interleaved, up to maxParallel)
  ca_worker_wave_ended
  ca_orchestration_scorecard                  (only when collect: "gate")
ca_session_ended
```

| Event | Payload (beyond `kind`/`sessionId`/`waveId`) | Emitted at |
|---|---|---|
| `ca_worker_wave_started` (`sse-events.ts:239-246`) | `mode: "roster"\|"supervised"`, `isolation: "worktree"\|"none"`, `maxParallel: int>0`, `planned: [{id, use?, role?, task?}]` | Once per fan-out. **Roster/clone path: only AFTER the crew match has run** (`roster-run.ts:186-200`) — that is when `entry.id ?? crew.name` becomes authoritative; announcing parsed-roster ids would name workers that dispatch then renames, and a client keying rows on them would never match a later event. **Worktree path: ids are positional** (`entry.id ?? f"w{i+1}"`, `worktree-dispatch.ts:85`) so it can announce up front — but the helper must derive them *exactly* as the pool does |
| `ca_worker_started` (`sse-events.ts:254-266`) | `workerId`, `workerSessionId` (**the join key**), `role?`, `use?`, `task?`, `engine?`, `model?`, `scope?` | As each worker's session is **created**, so it respects `maxParallel` (`spawn-child.ts:175-189`, `worktree-dispatch.ts:207-215`). Never emitted for a worker whose session creation failed — there is no transcript to join to |
| `ca_worker_completed` (`sse-events.ts:278-290`) | `workerId`, `workerSessionId` (`""` when the worker died before its session existed), `status` (open string, not an enum), `error?`, `text?`, `artifacts?`, `usage?`, `durationMs?` | From the **dispatch loop**, not inside the spawn function, so no return path can skip it (`spawn-child.ts:233-245`). Worktree path emits it from inside the pool because `artifacts` is only available there (`worktree-dispatch.ts:231-243`) |
| `ca_worker_wave_ended` (`sse-events.ts:326-339`) | `dispatched`, `unmatched?`, `integrated?`, `conflicts?`, `aborted?`, `budget?` | After the merge and cleanup, so `integrated`/`conflicts` are final (`worktree-dispatch.ts:274-282`). **Emitted even when nothing dispatched** — a client must treat "started, then ended with `dispatched: 0`" as a reportable outcome, not a missing event |
| `ca_orchestration_scorecard` (`sse-events.ts:349-374`) | `verdict: "pass"\|"fail"`, `scorecard: {agent, status, workers[], integrated, conflicts, budget?, gate{verdict, reasons}}` | Only under `collect: "gate"` (`roster-run.ts:334-347`). Its absence under `into-context` is not a failure |

`createWaveReporter` (`wave-events.ts:59-79`) frames each payload with `sessionId` + `waveId` and **wraps every emit in try/except**: "a dropped event costs a UI affordance; a thrown one would cost the wave — and the workers are the expensive part of the run" (`wave-events.ts:49-58`).

The wave id is `f"wave_{n}"`, counted **per session, not per turn** (`roster-run.ts:311-318`), so turn 2's fan-out is `wave_2`. Worker ids repeat across waves (`w1` again) and the wave id is what keeps the rows apart.

### 6.2 Where they surface in Python

`EngineEvent.kind` is a closed `Literal` of five values (`protocol_types/__init__.py:109-115`) and `_stream_engine_events` raises `HarnessProtocolError` on anything unrecognised (`coordinator.py:509`).

**Recommendation: add the five kinds to the `Literal`** and a single `payload: dict` field carrying the event body:

```python
kind: Literal[
    "session_started", "sdk_message", "usage_snapshot",
    "permission_request", "session_ended",
    "worker_wave_started", "worker_started",
    "worker_completed", "worker_wave_ended", "orchestration_scorecard",
]
```

Names drop the `ca_` prefix, which is the TS *wire* prefix; `EngineEvent.kind` values in this SDK are bare (`sdk_message`, not `ca_sdk_message`). The wire-shaped names appear only in the serialized payload for parity, if anyone forwards them.

These are `yield`ed from `_stream_engine_events` alongside engine events, so a consumer sees them on the `ChatHandle` stream in the guaranteed order. `ChatHandle._iterate` (`harness/__init__.py:105-113`) passes everything through untouched; `state.messages` must **not** collect them (only `sdk_message` payloads belong in `ChatResult.messages`).

The lower-churn alternative — one new kind `"orchestration"` with `payload["kind"]` discriminating — avoids touching the `Literal` that `tests/test_isinstance_parity.py` and `tests/test_surface_parity.py` cover. It is defensible; I prefer the explicit five for discoverability and because a `match ev.kind` on the consumer side is the idiom this dataclass was designed for. Either way `coordinator.py:509`'s `else: raise` must learn the new kinds.

### 6.3 Ingest — the important negative result

`KNOWN_KINDS` in `packages/agentos-server/src/routes/ingest.ts:13-30` is exactly:

```
session_started, user_message, assistant_message, system_message,
tool_use, tool_result, thinking, truncated, usage_snapshot,
session_ended, policy_decision
```

**No `ca_worker_*` and no `ca_orchestration_scorecard`.** And an unlisted kind is **skipped, not rejected** — `if (!KNOWN_KINDS.has(kind)) return null;` (`ingest.ts:91`), presenting as a cheerful `200` with `skipped: n` (`ingest.test.ts:3`). So:

> **Do not emit wave events as `TelemetryEvent`s through `Pipeline.emit`.** They would be POSTed, accepted with a 200, and silently discarded — the worst possible failure shape. Wave events are a **local `ChatHandle` stream affordance only**, until `KNOWN_KINDS` is extended server-side.

This costs nothing, because **each worker's own session already reaches ingest**: the worker coordinator is constructed with `parent_session_id` / `parent_agent_id`, `_emit` stamps them onto every event (`coordinator.py:596-600`), and `AgentOSHttpSink._serialize` puts them on the wire (`telemetry/sinks/agentos_http.py:149`). That is the same mechanism `tests/test_fork.py:284-300` already asserts, and it is exactly TS's story (`wave-events.ts:13-16`: "worker sessions have always been fully observable *after* the fact"). What wave events add is the **live** view — the 30–120 s silent gap between the orchestrator's text and `session_ended` — and that is a local-stream concern.

If the console gap matters, the follow-up is a server-side PR adding the five kinds to `KNOWN_KINDS` plus projections; that is out of scope here and belongs in §10.5.

---

## 7. Module layout

New package `src/computeragent/harness/orchestration/`. Column **P** = pure logic (direct port, no I/O), **G** = git CLI, **L** = lifecycle plumbing (touches the coordinator / event loop).

| Module | Ports | P/G/L | Contents |
|---|---|---|---|
| `__init__.py` | — | P | Re-exports the public names. |
| `spec.py` | `protocol/src/orchestration.ts` | P | `WorkerScope`, `WorkerMcpPolicy`, `WorkerCrewEntry`, `OrchestrationSpec`, `OrchestrationLimits` (from `ForkConfig`), `DispatchRequestItem`/`DispatchResultItem`, `ORCHESTRATION_SUPERVISED_DISPATCH_MANY_TOOL_NAME`, `ORCHESTRATION_INTERNAL_TOOLS`, `is_orchestration_internal_tool`, `spec_from_agent_doc()` (the `GET /v1/agents/{id}` `orchestration` field → `OrchestrationSpec`, applying `loader.ts:86-105`'s defaults). |
| `crew.py` | `identity-gitagentprotocol/src/subagents.ts` + `harness-server/src/services/crew.ts` | P | `classify_gap_agents(workdir) -> GapAgents`, `crew_entry_to_worker_spec`, `use_to_source`. `identity/gap_subagents.py:45` is rewritten to delegate here. |
| `path_segment.py` | `harness-server/src/path-jail.ts:47-85` | P | `SAFE_SEGMENT` regex, `is_safe_path_segment`, `assert_safe_path_segment`, `to_safe_path_segment`, `PathEscapeError`. |
| `parse_roster.py` | `parse-roster.ts` | P | `RosterEntry`, `RosterOutcome`, `RosterParse`, `parse_roster_result`, `parse_roster`, `is_roster_text`, `_normalize_ids`, `_extract_json_block`. |
| `contract.py` | `roster-contract.ts` | P | `ROSTER_CONTRACT`, `append_roster_contract(options)`. Templated on `fork.py:172-181`. |
| `budget.py` | `budget.ts` | P | `MIN_VIABLE_USD`, `CostSource`, `BudgetExceeded`, `Settlement`, `BudgetSummary`, `BudgetAllocator`, `warn_unenforceable_budget`. |
| `scorecard.py` | `scorecard.ts` | P | `GateResult`, `ScorecardWorker`, `Scorecard`, `evaluate_gate`, `build_scorecard`. |
| `digest.py` | `worker-digest.ts` | P | `build_synthesis_prompt`, `build_direct_answer_prompt`, the two char caps. |
| `worker_mcp.py` | `worker-mcp.ts` | P | `resolve_worker_mcp`, `inherit_mcp_into`. Single-engine simplification per §3.5. |
| `scope_guard.py` | `scope-guard.ts` + `create-session.ts:257-287` | P (+ glob dep) | `ScopeGuardDecider`, `AllowAllDecider`, `CompositeDecider`, `_matches_any` (wcmatch). Returns `claude_agent_sdk` permission results. |
| `events.py` | `wave-events.ts` + `sse-events.ts:207-374` | L | `WaveReporter` + the five payload dataclasses; each emit try/excepted. |
| `worktree.py` | `worktree.ts` | **G** | `EXCLUDES`, `WorkerTree`, `_git`, `init_baseline`, `create_worktree`, `diff_paths`, `commit_worker`, `integrate`, `cleanup`, `restore_baseline_paths`. |
| `dispatch.py` | `spawn-child.ts` | L | `WorkerSpec`, `WorkerUsage`, `WorkerResult`, `SessionOutcome`, `spawn_worker`, `dispatch_workers`, `collect_session`. Builds a sibling `HarnessCoordinator` per worker. |
| `worktree_dispatch.py` | `worktree-dispatch.ts` | L + G | `WorktreeRunResult`, `dispatch_worktree_roster`, `dispatch_worktree_entries`, `_worker_options`, `INLINE_IDENTITY_PATHS`. |
| `runner.py` | `roster-run.ts` | L | `dispatch_roster`, `dispatch_roster_entries`, `run_wave`, `wave_needs_synthesis`, `gate_applies`, `no_reports_to_fold`, `direct_answer_reason`, `match_crew`, `synthesis_enabled`, `WaveHandle`. |
| `dispatch_tool.py` | `engine-claude-agent-sdk/dispatch-tool.ts` (referenced from `orchestration.ts:126,190-192`) | L | Builds the in-process `orchestration` MCP server exposing `dispatch_many`. Borrows `fork.py:217-272`. |

**Files outside the package that change:**

| File | Change |
|---|---|
| `src/computeragent/harness/coordinator.py` | `_ensure_booted` (`:157`): read `orchestration` off the resolved agent doc, build the spec, append the roster contract in `roster` mode, inject `dispatch_many` in `supervised` mode (replacing the fork block at `:210-240`). `run_chat` (`:309`): pass `close=` to `_enqueue_user_message` (`:348`). `_stream_engine_events` (`:412`): the withhold/wave/synthesis logic of §5.3, plus new kinds in the `else: raise` at `:509`. `dispose` (`:243`): cancel the wave task in place of `_fork_context.teardown()` (`:250-252`). `_SessionState` (`:40`): `wave_task`, `wave_seq`. New helper `_build_worker_coordinator`. |
| `src/computeragent/protocol_types/__init__.py` | Five new `EngineEvent.kind` literals (`:109-115`). |
| `src/computeragent/harness/__init__.py` | `ComputerAgent.__init__` (`:160`): drop `fork_mode`/`fork_config`/`_fork_depth`, add `orchestration_limits` / `wave_timeout_s` / `follow_up_timeout_s` passthroughs (`:216`, `:408`). `ChatHandle.cancel` (`:139`): cancel the wave. `ChatHandle` docstring: full-drain requirement. |
| `src/computeragent/identity/gap_subagents.py` | `load_gap_subagents` delegates to `orchestration.crew.classify_gap_agents`. |
| `src/computeragent/engines/claude_agent.py` | No change expected. Verify `_build_options`' field filter (`:89-94`) does not silently drop `max_budget_usd`/`max_turns` for workers. |
| `pyproject.toml` | New extra `orchestration = ["wcmatch>=8.5,<11"]`, folded into `all`; new pytest marker `orchestration_git`. |
| **Deleted** | `src/computeragent/harness/fork.py`, `tests/test_fork.py`. |

---

## 8. Phased implementation plan

Each phase is independently shippable and testable. Estimates are engineering-days for one developer already familiar with both codebases; add ~40% otherwise.

### Phase 0 — Remove `fork.py`, land the spec (1.0 d)
Delete `harness/fork.py` + `tests/test_fork.py` and the coordinator's fork block (`coordinator.py:96-99,118-123,210-240,250-252`) and `ComputerAgent`'s fork kwargs. Add `orchestration/spec.py` + `orchestration/crew.py` + `orchestration/path_segment.py`; rewire `identity/gap_subagents.py`. Nothing dispatches yet; `spec_from_agent_doc(None)` returns `None` and a plain agent runs byte-identically.
**Ships:** the agent doc's `orchestration` field is parsed and exposed on the coordinator. **Test:** spec defaults, crew classification, `use:`-wins, `use_to_source`, id slugification.
**Risk:** the `fork.py` deletion is a breaking change for anyone on 0.3.0 `fork_mode=`. Needs a CHANGELOG entry and a `TypeError`-with-guidance shim for one minor version.

### Phase 1 — Pure-logic core (1.5 d)
`parse_roster.py`, `contract.py`, `budget.py`, `scorecard.py`, `digest.py`, `worker_mcp.py`. All pure functions, all directly testable, nothing wired.
**Ships:** nothing user-visible. **Test:** four suites, 17 + 7 + 6 + 15 cases (§9). This is the highest test-value-per-hour phase and the one where TS parity is easiest to verify line by line.

### Phase 2 — Worktree layer (1.5 d)
`orchestration/worktree.py` against a real temp git repo. No dispatch yet — the functions are called directly by tests.
**Ships:** nothing user-visible. **Test:** 9 cases + a `git` preflight skip. **Risk:** git version differences on `worktree prune` / `-B` semantics; pin a minimum git in the preflight (`--force -B` on `worktree add` needs ≥ 2.23).

### Phase 3 — Scope guard + composite decider (1.0 d)
`orchestration/scope_guard.py`, the `wcmatch` extra, and the glob-parity table. Wire `CompositeDecider` so a worker's guard composes with an inherited parent decider.
**Ships:** `ScopeGuardDecider` is usable standalone as a `ComputerAgent(policy_decider=...)`. **Test:** 10 + a minimatch-parity table + a `bypassPermissions` end-to-end that proves the `PreToolUse` hook path fires (§3.6).

### Phase 4 — Dispatch, isolation `none` (2.0 d)
`orchestration/dispatch.py`: `spawn_worker` builds a sibling `HarnessCoordinator`, `dispatch_workers` runs the bounded pool, `collect_session` drains a worker's stream into `SessionOutcome`. Parent linkage, envs, MCP, policy-instance, budget cap, model fallback.
**Ships:** a programmatic `dispatch_workers(...)` API, callable from a script, with no turn-lifecycle changes. **Test:** fan-out width, fail-soft on a worker that raises, linkage stamping, budget settlements, MCP inheritance end-to-end.

### Phase 5 — Wave events (0.5 d)
`orchestration/events.py`, the five `EngineEvent` kinds, the `else: raise` update, per-emit try/except, wave-id sequencing.
**Ships:** `dispatch_workers` emits events into a supplied sink. **Test:** 13 cases.

### Phase 6 — Turn lifecycle + roster mode (2.5 d) ← the risky one
The §5.3 edits to `coordinator.py`, plus `orchestration/runner.py` (`dispatch_roster`, `run_wave`, the three predicates), plus `orchestration/worktree_dispatch.py`. End-to-end `mode: "roster"` with `isolation: "none"` **and** `"worktree"`.
**Ships:** the feature. **Test:** 15 synthesis cases against a fake engine (§9), plus the two watchdogs, plus the `empty-roster` silence.
**Risk:** highest. The withhold logic sits in the middle of a 130-line `async for` that already handles four event kinds and three telemetry paths. Budget a day of the 2.5 for restructuring `_stream_engine_events` into smaller helpers *first*, as a no-behaviour-change refactor with the existing `tests/test_coordinator_ttft.py` / `tests/test_harness_protocol.py` as the guard.

### Phase 7 — `collect: "gate"` + scorecard emission (0.5 d)
Wire `gate_applies` + `build_scorecard` + the scorecard event into `run_wave`.
**Ships:** gate verdicts on the stream. **Test:** gate fires under `gate`, absent under `into-context`, absent for `empty-roster`.

### Phase 8 — Supervised mode (1.5 d)
`orchestration/dispatch_tool.py` + the injection in `_ensure_booted` + the policy short-circuit for `ORCHESTRATION_INTERNAL_TOOLS` in `_build_permission_resolver` (`coordinator.py:604+`), with the `policy.control_plane_bypass` log and a decision event that deliberately omits `outcome` (`orchestration.ts:167-177`).
**Ships:** `mode: "supervised"`. **Test:** the tool bypasses the decider; reports return as the tool result; wave events still emit; the queue still closes normally.

**Total: ~12 engineering-days.** Phases 0–3 (5 d) are low-risk and can land as one release; 4–5 (2.5 d) as a second; 6–7 (3 d) as the headline; 8 (1.5 d) as a follow-up.

---

## 9. Test plan

Conventions from `pyproject.toml`: `asyncio_mode = "auto"` (so no `@pytest.mark.asyncio` is needed — note `tests/test_fork.py` uses it redundantly; the new suites should not), `testpaths = ["tests"]`, `--strict-markers --strict-config`, existing marker `integration`. Add one marker:

```toml
markers = [
  "integration: requires real Claude CLI and credentials",
  "orchestration_git: requires a real `git` binary on PATH",
]
```

Fakes are lifted from `tests/test_fork.py:33-68`: `_FakeEngine` / `_FakeSubstrate` / `_FakeLoader` / `_CapturePipeline`. The key upgrade is a **scriptable** fake engine — `tests/test_fork.py`'s never yields — that emits a caller-supplied list of `EngineEvent`s per turn and reads the user queue between turns, so the synthesis turn is observable. Put it in `tests/conftest.py` beside `make_upstream_stream` (`tests/conftest.py:56-67`) as `scripted_engine_factory`. `respx` (already a dev dep) covers any `GET /v1/agents/{id}` and SRS calls.

| New file | Mirrors | Cases | Notes |
|---|---|---|---|
| `tests/test_orchestration_spec.py` | `loader.ts:86-105` | ~8 | Every default from the §2.1 table; `None` for a plain agent; `budget_usd` falling back to `runtime.budget_usd`; `worker_mcp` absent ⇒ `"inherit"` via `resolve_worker_mcp`, never read directly. |
| `tests/test_orchestration_crew.py` | `subagents.ts` + `crew.ts` + `crew.test.ts` | ~12 | `use:` wins over an inline `description`/`prompt`; nested dir needs `SOUL.md`; malformed YAML skipped without breaking siblings; `separate_session` defaults `True`; `use_to_source` for all six forms; `load_gap_subagents` parity with today's output (reuse `tests/test_gap_subagents.py` fixtures). |
| `tests/test_parse_roster.py` | `parse-roster.test.ts` | **17** | The 8 `parseRoster` cases (fenced `workers`; `roster` key; **greedy fence surviving embedded ```` ``` ```` — the neuron-lyzr bug**; bare object; entries with no task skipped; no-block/malformed → `[]`; inline `role`/`scope`/`model`/`tools`; an `mcp` key in a roster entry ignored) + the 9 outcome cases (`empty-roster` for `workers: []` and for `roster: []`; `no-block`; `invalid-json`; `no-usable-entries`; `not-a-roster`; `entries`; `is_roster_text` marks exactly four; `parse_roster` still returns just entries). Plus 3 id-normalization cases (prose id slugified; `"***"` dropped to the positional fallback; two prose ids not colliding onto one worktree). |
| `tests/test_orchestration_budget.py` | `budget.test.ts` | **7** | `slice(n) == min(per_worker, remaining/n)`; recompute after settlements; `check_before_dispatch` returns cap or raises; `exhausted()` below the floor; negatives clamped + `estimated_share`; `summary()` rounding (4 dp, 3 dp for the share); `MIN_VIABLE_USD == 0.05`. |
| `tests/test_orchestration_scorecard.py` | `scorecard.test.ts` | **6** | Pass on clean; the four failure reasons (conflict names worker + paths; worker errored/incomplete; budget abort; **zero workers with no abort**); `build_scorecard` summarizes artifacts-as-count/integration/budget/gate; `status` mirrors the verdict. |
| `tests/test_orchestration_worktree.py` | `worktree.test.ts` | **9** + 2 | `pytest.mark.orchestration_git`, `skipif(shutil.which("git") is None)`, real repo under `tmp_path`. `init_baseline` makes a repo and returns a sha; unsafe `worker_id` rejected (`../escape`, `a/b`, `..`); exact attribution including a nested path; non-overlapping branches integrate cleanly; two workers on one file produce a recorded conflict **and** `merge --abort` leaves the integration branch usable; `commit_worker` returns `False` on no change; `cleanup` removes the trees; **re-create with the same worker id after cleanup** (the `-B` bug); **re-create when cleanup never ran** (the `--force` case). Plus two net-new: `EXCLUDES` keeps `agent.yaml` and `__pycache__/` out of `diff_paths`; `restore_baseline_paths` restores a pre-existing `agent.yaml` and *deletes* one the baseline lacked. |
| `tests/test_orchestration_scope_guard.py` | `scope-guard.test.ts` | **10** + table | Non-write tools allowed; in-scope write via relative *and* absolute path; out-of-scope denied with a helpful reason; workspace escape denied; all four write-tool names case-insensitively and all four path fields; unrecognized target allowed; empty scope denies everything; `always_allow` exempts. Plus `test_wiring`: composed with an inherited decider, deny-if-any, scope guard first. Plus a **minimatch-parity table** asserting `src/*.py` does NOT match `src/a/b.py` and `*.py` does NOT match `src/a.py` — the two `fnmatch` would get wrong. Plus one `bypassPermissions` test proving the `PreToolUse` hook (`engines/claude_agent.py:120-130`) carries the denial. |
| `tests/test_orchestration_worker_mcp.py` | `worker-mcp.test.ts` | **~18** (of 26) | `resolve_worker_mcp` precedence (entry > spec > `"inherit"`); `"none"` returns the same object; no parent servers ⇒ unchanged; explicit list selects; unknown name skipped not fatal; in-process SDK server skipped; inherited entry wins a key collision; `allowed_tools` extended but **never created**; server-level grant `mcp__<name>` added. The 8 omitted cases are the gitclaw `<server>__<tool>` / `?tools=` enumeration branch, which does not exist in this SDK — record the omission in the module docstring. |
| `tests/test_orchestration_events.py` | `wave-events.test.ts` | **13** | Worktree wave bracketed start/end with each worker reported once; announced ids are the ids dispatch uses; `worker_session_id` present and joinable; per-worker artifacts on its own completion; conflicts on the wave *after* the merge; a wave that dispatched nobody still brackets; a budget abort ends without dispatching; a reporter whose sink raises does not kill the fan-out; no reporter ⇒ no events; delivered on the orchestrator's own stream before it ends; gate verdict surfaces under `collect: "gate"`; no scorecard under `into-context`; each turn's fan-out numbered as its own wave. |
| `tests/test_orchestration_synthesis.py` | `roster-synthesis.test.ts` | **15** | The lifecycle. Roster text withheld and replaced by the synthesis answer; prose left exactly as written (no extra turn); `empty-roster` takes a turn but with the **direct-answer** prompt; `unparsed-roster` likewise; `aborted` forces a turn; `unmatched` forces a turn; the synthesis turn does **not** re-enter the wave (no second fan-out); `is_error` on the primary result declines the follow-up and releases the error terminator; `ORCHESTRATOR_SYNTHESIS=0` disables it; the engine stream ending mid-synthesis releases the withheld result immediately; the follow-up watchdog releases it after the timeout; the wave watchdog terminates a wedged wave; digest truncation at 8 000 / 60 000 chars; the digest names a failed worker rather than pretending; `_lifecycle_last_result_payload` is the **synthesis** result, not the roster. |
| `tests/test_orchestration_dispatch.py` | `spawn-child.test.ts` + `worktree-dispatch.test.ts` | ~12 | Pool width respects `max_parallel`; a worker that raises comes back `status="errored"` without losing the wave; parent linkage stamped on worker telemetry (assert via `_CapturePipeline`); the parent's decider **object** is the one the worker got (identity check — this is the one-SRS-fetch guarantee); model falls back to the parent's; envs merged; `unattended` ⇒ `bypassPermissions`; crew match precedence (`use` → `id` → crew `use` → single-crew fallback, `roster-run.ts:452-462`); unmatched entries reported. |
| `tests/test_orchestration_contract.py` | `roster-contract.test.ts` | ~5 | All four prompt shapes of §2.4; the GAP `append` text survives; non-dict options untouched. |

**Roughly 130 new cases.** Existing suites that must stay green untouched: `test_harness_protocol.py`, `test_coordinator_ttft.py`, `test_surface_parity.py`, `test_isinstance_parity.py`, `test_gap_subagents.py`, `test_gap_loader_parity.py`, `test_client_lifecycle.py`.

One `@pytest.mark.integration` end-to-end (opt-in, real CLI): a 2-worker worktree roster in a temp repo, asserting two non-empty artifact sets, a clean merge, and a synthesis answer that mentions both workers.

---

## 10. Open questions

These are stated rather than guessed. Each needs a decision before the phase that depends on it.

**10.1 — Where does the orchestrator's workspace come from in single-run mode?** (blocks Phase 6)
`isolation: "worktree"` makes the orchestrator's workdir a git repo and commits everything in it (`worktree.ts:97-99`). In single-run mode the workspace is whatever `LocalSubstrate.boot` produced from the resolved agent doc's source — which for a git source is a `--depth 1` clone in a temp dir (`substrates/local.py:51-56`), i.e. **already a git repo with its own history and remote**. `init_baseline` skips `git init` when `.git` exists (`worktree.ts:90`) and commits on top of the cloned HEAD, leaving the run's branch state mutated relative to the clone. That is fine for a throwaway clone and *not* fine when `source` is a caller-owned `Path` (`substrates/local.py:48-49`) — we would be committing to the user's own working repo on the `orchestration-baseline` branch. **Decide:** refuse `isolation: "worktree"` on a caller-owned `Path` source; or always copy the workspace to a temp dir first; or require an explicit opt-in flag. My lean is refuse-with-a-clear-error plus an `allow_worktree_in_place=True` escape hatch, but this is a product call.

**10.2 — Does a worker inherit the run's `agent_id`, or get a synthetic one?**
`fork.py:320` minted `f"{parent_id}/{child_name}#{uuid4().hex[:6]}"`. TS uses `parentAgentId: orchestrator.identity.name` and lets the worker carry its own identity name (`spawn-child.ts:108`). In single-run mode `agent_id` is the AgentOS registry key, and minting `parent/w1#abc123` writes a registry row for an agent that does not exist. **Decide:** whether inline workers should carry the parent's `agent_id` (clean registry, ambiguous rows) or a synthetic one (clean rows, registry noise), and whether AgentOS's ingest projection tolerates an unregistered `agent_id`.

**10.3 — Does a bare `use:` value resolve through `GET /v1/agents/{id}`?**
`crew.ts:38-41` explicitly defers this ("that needs the AgentOS registry (Phase 6)") and treats a bare id as a git URL that will fail to clone. Single-run mode makes the resolution trivially available. **Decide:** resolve it (nicer, and the obvious thing a user expects from `use: my-other-agent`) or keep TS parity and defer. If resolved, the worker's own API key / group scoping needs a decision too.

**10.4 — Is `wcmatch` an acceptable new dependency?**
§3.6 argues it is required for correct scope-guard semantics and that the alternative is a hand-rolled matcher with known brace-expansion drift. It would be the first non-optional-extra glob dependency in the package. **Decide:** new extra `orchestration` (my recommendation) vs. hand-rolled vs. accepting `fnmatch`'s wider-than-declared scopes with a documented caveat.

**10.5 — Should `KNOWN_KINDS` be extended server-side?** (§6.3)
Today the five wave events cannot reach AgentOS at all, and the local stream is the only consumer. Extending `packages/agentos-server/src/routes/ingest.ts:13-30` plus the projections in `stores/telemetry-projection.ts` would give the console the live view. That is a cross-repo change with its own review; it is listed here so the omission is deliberate rather than forgotten.

**10.6 — What is the right default for `max_depth`?**
Nothing in the TS subsystem prevents a worker that is itself an orchestrator from fanning out again — a `use:`-referenced worker whose own repo declares `orchestration:` will recurse without bound. `fork.py:75` defaulted to `2`. **Decide:** carry a depth counter on the worker coordinator (env `COMPUTERAGENT_ORCHESTRATION_DEPTH`, as `fork.py:352` did) and refuse a wave past the cap, and pick the default. My lean: depth `1` by default (workers may not fan out), raisable — the opposite of `fork.py`'s permissive `2`, because a wave-of-waves multiplies the budget by `max_parallel²`.

**10.7 — Do wave events belong in `ChatResult`?**
`ChatHandle._finalize_result` (`harness/__init__.py:147-156`) snapshots `state.messages`, which holds only `sdk_message` payloads. A caller who does `await agent.chat(...)` without iterating gets no wave information at all. **Decide:** add a `wave: WaveSummary | None` field to `ChatResult` (nice, but widens a frozen public dataclass) or require iteration for wave visibility.

**10.8 — Does `supervised` mode need the withhold machinery at all?**
§5.3 says no: its workers run inside a tool call, so the queue can close normally and the reports arrive as the tool result. That holds for the flow as written, but `roster-run.ts:413-439` runs the wave inside a hook that has no `push`, and the two modes share `dispatch_roster_entries`. Worth re-confirming against the real `dispatch-tool.ts` (not read for this plan — it lives in `packages/engine-claude-agent-sdk/`) before Phase 8.
