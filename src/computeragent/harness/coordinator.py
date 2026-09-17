"""Orchestrator — wires engine ↔ substrate ↔ session-store and converts
every :class:`EngineEvent` into the existing :class:`TelemetryEvent` shape
so OTel + AgentOS sinks light up without harness-side awareness.

The coordinator owns the telemetry hot path in harness mode. Engines
themselves must NOT call ``pipeline.emit`` — otherwise events double-fire
when the engine is composed under the harness. (The 0.1.x ``_proxy.query``
path retains its own pipeline emit for standalone drop-in use.)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..orchestration.wave_events import WAVE_EVENT_KINDS, WaveReporter
from ..protocol_types import (
    EngineContext,
    EngineDriver,
    EngineEvent,
    IdentityLoader,
    IdentitySource,
    PermissionDecision,
    PermissionRequest,
    PolicyDecider,
    SessionStore,
    Substrate,
)
from ..telemetry import TelemetryEvent, get_pipeline, new_session_id
from ..telemetry.pipeline import Pipeline
from .errors import HarnessError, HarnessProtocolError
from .usage import UsageRollup

logger = logging.getLogger("computeragent.harness")


class _SessionState:
    """Per-chat-session bookkeeping shared between the coordinator and the
    :class:`computeragent.harness.ChatHandle` it returns."""

    def __init__(
        self,
        *,
        session_id: str,
        agent_name: str | None,
        agent_description: str | None,
        pipeline: Pipeline,
    ) -> None:
        self.session_id = session_id
        self.agent_name = agent_name
        self.agent_description = agent_description
        self.pipeline = pipeline
        self.user_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.cancel_event = asyncio.Event()
        self.pending_permissions: dict[str, asyncio.Future[PermissionDecision]] = {}
        self.usage = UsageRollup()
        self.messages: list[Any] = []
        self.started_at = time.monotonic()
        # Carried across engine turns: a roster orchestrator takes a second
        # (the closing synthesis), and what the run REPORTS must be that turn's
        # answer, not the roster the first one wrote.
        self.last_assistant_text: str = ""
        self.result_payload: Any = None
        # Wave events produced by a fan-out, waiting to ride out on this
        # session's stream. A supervised fan-out fills this while the engine
        # loop is parked on a tool call; the roster path drains it live.
        self.wave_events: asyncio.Queue[EngineEvent | None] = asyncio.Queue()


class HarnessCoordinator:
    """Single-session coordinator.

    One ``HarnessCoordinator`` per ``ComputerAgent`` instance. ``run_chat``
    is invoked once per ``.chat(...)`` call; it composes a user-message
    queue (which the engine consumes via async-iter) and yields engine
    events back to the caller.
    """

    def __init__(
        self,
        *,
        engine: EngineDriver,
        substrate: Substrate,
        identity_loader: IdentityLoader,
        source: IdentitySource,
        envs: dict[str, str],
        session_store: SessionStore | None = None,
        session_id: str | None = None,
        agent_name: str | None = None,
        agent_description: str | None = None,
        agent_id: str | None = None,
        policy_decider: PolicyDecider | None = None,
        on_permission_request: (
            Callable[[PermissionRequest], Awaitable[PermissionDecision]] | None
        ) = None,
        budget_max_usd: float | None = None,
        telemetry_pipeline: Pipeline | None = None,
        project_key: str = "default",
        options_overlay: dict[str, Any] | None = None,
        # ── cloning a private source ────────────────────────────────────────
        # The PAT used to clone a private source, when there is one.
        git_token: str | None = None,
        mcp_bindings: tuple[Any, ...] = (),
        # The registry's own `orchestration` block, layered over whatever the
        # identity loader declares. See `_apply_orchestration_overrides`.
        orchestration_overrides: dict[str, Any] | None = None,
        # False for a coordinator that IS a worker. See `_orchestration`.
        drive_orchestration: bool = True,
        mcp_client: Any | None = None,
        mcp_bridge_servers: Any | None = None,
        # Linkage for a child session spawned by a parent agent. Stamped onto
        # every emitted event so a console can nest the child under its parent.
        parent_session_id: str | None = None,
        parent_agent_id: str | None = None,
    ) -> None:
        self._engine = engine
        self._substrate = substrate
        self._identity_loader = identity_loader
        self._source = source
        self._envs = envs
        self._session_store = session_store
        self._session_id_override = session_id
        self._agent_name = agent_name
        self._agent_description = agent_description
        # Stable agent identifier (telemetry/registry only — never affects
        # execution). Stamped onto every emitted event via ``_emit``.
        self._agent_id = agent_id
        self._parent_session_id = parent_session_id
        self._parent_agent_id = parent_agent_id
        self._policy_decider = policy_decider
        self._on_permission_request_user = on_permission_request
        self._budget_max_usd = budget_max_usd
        self._pipeline = telemetry_pipeline or get_pipeline()
        self._project_key = project_key
        # Explicit kwargs the user supplied via ``ComputerAgent(options=...)``
        # / ``model=...``. Layered on top of whatever the identity loader
        # resolves so caller intent always wins.
        self._options_overlay: dict[str, Any] = dict(options_overlay or {})
        self._git_token = git_token
        # MCP servers bound on the agent, and the client that reaches them.
        # Attached at boot rather than at construction because discovery is
        # a network call and construction is synchronous.
        self._orchestration_overrides = dict(orchestration_overrides or {})
        self._drive_orchestration = drive_orchestration
        self._mcp_bindings = mcp_bindings
        self._mcp_client = mcp_client
        self._mcp_bridge_servers = mcp_bridge_servers

        # Substrate state — booted lazily on first chat.
        self._substrate_handle: Any = None
        self._workspace_path: Path | None = None
        self._manifest: Any = None
        self._disposed = False
        # ── lifecycle state ─────────────────────────────────────────────
        # One ``ComputerAgent`` instance == one logical session. The
        # coordinator fires ``session_started`` exactly once (on the
        # first chat) and ``session_ended`` exactly once (on dispose) so
        # AgentOS / Mongo sinks treat N chats as turns of one
        # conversation, NOT N separate sessions. Per-chat user prompts
        # land via ``user_message`` events; assistant text via
        # synthesised events from the engine's sdk_message yields.
        self._lifecycle_session_id: str | None = session_id
        self._lifecycle_started: bool = False
        self._lifecycle_first_prompt: str = ""
        self._lifecycle_started_at: float = 0.0
        self._lifecycle_usage: UsageRollup = UsageRollup()
        self._lifecycle_last_assistant_text: str = ""
        self._lifecycle_last_result_payload: Any = None
        # The engine's own session id from the last turn. One ComputerAgent is
        # one conversation, so each turn after the first resumes from this
        # rather than starting a fresh engine session with no memory.
        self._engine_session_id: str | None = None
        # One ComputerAgent is one conversation, and since turns now resume the
        # previous engine session, two in flight at once would interleave into
        # the same session, the same workspace and the same session-store key.
        # It used to "work" by luck; refuse it instead of corrupting a
        # transcript.
        self._turn_in_flight = False
        # ── orchestration state ─────────────────────────────────────────
        # One counter per SESSION, not per turn: a second chat's fan-out is
        # `wave_2`. Worker ids repeat across waves (`w1` again), so the wave id
        # is what keeps the two waves' rows apart on the caller's side.
        self._wave_seq = 0
        # Set by `_run_wave` when the fan-out wants one more turn to write the
        # answer; consumed immediately by the turn loop.
        self._pending_follow_up: str | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def _git_credential(self) -> dict[str, str] | None:
        """Auth for cloning the source, or ``None`` for an anonymous clone.

        Just the explicit token now. There used to be a middle step that asked
        AgentOS to mint a GitHub-App installation token, but that route is
        App-only and these deployments authenticate with PATs — held either
        directly on the agent or in the Lyzr credential vault behind its
        `credential_id`, neither of which the App route can return. So the
        fallback could not succeed, and all it did was turn a missing token
        into a slower failure.

        The token is handed to the substrate as a credential mapping, not
        spliced into the clone URL, so it reaches neither argv nor the remote
        recorded in ``.git/config``.
        """
        if not (isinstance(self._source, str) and self._git_token):
            return None
        from ..substrates.local import _host_of

        return {
            "host": _host_of(self._source),
            "username": "x-access-token",
            "token": self._git_token,
        }

    async def _ensure_booted(self) -> None:
        if self._disposed:
            # `dispose()` closes the substrate handle but leaves it set, so
            # without this the short-circuit below would sail past and run the
            # turn with `cwd` pointing at a temp directory that has been
            # removed — and because `_disposed` is already True, the closing
            # `dispose()` would be a no-op, so that turn would emit no
            # `session_ended` and clean nothing up.
            raise HarnessError(
                "this ComputerAgent has been disposed — build a new one rather than reusing it. "
                "Its workspace is gone and its session is closed."
            )
        if self._substrate_handle is not None:
            return
        git_credential = self._git_credential()
        self._substrate_handle = await self._substrate.boot(
            source=self._source, envs=self._envs, git_credential=git_credential
        )
        self._workspace_path = self._substrate_handle.workspace_path
        loaded = await self._identity_loader.load(
            source=self._source,
            target_engine=self._engine.name,
            workspace_path=self._workspace_path,
        )
        # Stamp the cloned commit SHA onto the manifest (if the loader didn't
        # already provide one) so it rides session_started → server SHA sync.
        revision = getattr(self._substrate_handle, "revision", None)
        if revision and not getattr(loaded, "sha", None):
            from dataclasses import replace

            loaded = replace(loaded, sha=revision)
        # Overlay caller-supplied options (from ComputerAgent kwargs) on
        # top of whatever the loader resolved. Caller intent wins.
        if self._options_overlay:
            merged_options = dict(loaded.options or {})
            merged_options.update(self._options_overlay)
            from dataclasses import replace

            loaded = replace(loaded, options=merged_options)
        loaded = await self._attach_mcp(loaded)
        # Loader-declared compliance hardening (e.g. GAP human-in-the-loop →
        # permission mode) runs AFTER the caller overlay, so strictest-wins
        # constraints hold even against caller-supplied options.
        harden = getattr(loaded, "harden", None)
        if callable(harden):
            from dataclasses import replace

            with suppress(Exception):
                hardened = harden(dict(loaded.options or {}))
                if isinstance(hardened, dict):
                    loaded = replace(loaded, options=hardened)
        loaded = self._apply_orchestration_overrides(loaded)
        loaded = self._attach_roster_contract(loaded)
        self._manifest = loaded

    def _apply_orchestration_overrides(self, loaded: Any) -> Any:
        """Layer the registry's ``orchestration`` block over the loader's spec.

        The two sources are not alternatives and neither can replace the other.
        The GAP repo is the only place that can name a worker CREW — ``use:``
        entries are files in that repo — while the registry is where an operator
        turns the knobs (parallelism, isolation, the stage budget) without a
        commit. So the repo supplies the crew and the defaults, and the registry
        overrides field by field.

        A registry block on an agent whose repo declares no orchestration at all
        still creates a spec: a worktree fan-out needs no crew, because its
        workers are defined inline by the roster the model writes.
        """
        if not self._orchestration_overrides:
            return loaded
        from dataclasses import replace

        from ..orchestration.contract import OrchestrationSpec

        spec = getattr(loaded, "orchestration", None) or OrchestrationSpec()
        overrides = self._orchestration_overrides
        changes: dict[str, Any] = {}
        for wire, field in (
            ("mode", "mode"),
            ("isolation", "isolation"),
            ("collect", "collect"),
            ("maxParallel", "max_parallel"),
            ("budgetUsd", "budget_usd"),
        ):
            if wire in overrides:
                changes[field] = overrides[wire]
        if not changes:
            return loaded
        logger.info("orchestration: registry overrides %s", sorted(changes))
        return replace(loaded, orchestration=replace(spec, **changes))

    def _attach_roster_contract(self, loaded: Any) -> Any:
        """Append the roster contract to a ``mode: "roster"`` orchestrator's
        system prompt.

        Runs AFTER ``harden`` on purpose. The contract is a property of the
        protocol this harness PARSES, not of the agent's own compliance posture,
        and hardening rewrites the options wholesale — so appending earlier
        risked a harden implementation dropping it, with the only symptom being
        an agent that occasionally answers a plain question in JSON.
        """
        spec = getattr(loaded, "orchestration", None)
        if spec is None or getattr(spec, "mode", None) != "roster":
            return loaded
        from dataclasses import replace

        from ..orchestration.roster_contract import append_roster_contract

        options = append_roster_contract(dict(loaded.options or {}), self._engine.name)
        return replace(loaded, options=options)

    async def _attach_mcp(self, loaded: Any) -> Any:
        """Merge the agent's MCP servers into the engine options.

        Deliberately a DEEP merge, unlike the overlay above. ``mcp_servers`` is
        a dict two independent sources write into — the identity loader (a GAP
        repo's ``gap_tools`` plus any ``agent.yaml`` HTTP servers) and this —
        and a shallow ``update`` would have one silently erase the other. The
        symptom is a GAP repo whose own tools vanish the moment an MCP server is
        bound to the agent, with nothing in the configuration looking wrong.

        Runs BEFORE the loader's ``harden`` hook, so a GAP repo's compliance
        constraints still get the last word over everything here.
        """
        if not self._mcp_bindings:
            return loaded
        if self._mcp_bridge_servers is None and self._mcp_client is None:
            return loaded
        if self._engine.name != "claude-agent-sdk":
            # Matches `applyMcpServers`, which refuses to write an
            # Anthropic-shaped allowlist for an engine that cannot read one:
            # gitclaw's allowedTools is a strict exact-match set with no
            # server-level form, so an `mcp__<server>` entry there matches
            # nothing and filters out EVERY tool, built-ins included.
            logger.warning(
                "%d MCP server(s) are bound but the %r engine has no MCP support in this SDK — "
                "running without them.",
                len(self._mcp_bindings),
                self._engine.name,
            )
            return loaded

        from dataclasses import replace

        from ..mcp.server import build_mcp_servers

        built = (
            self._mcp_bridge_servers
            if self._mcp_bridge_servers is not None
            else await build_mcp_servers(self._mcp_bindings, self._mcp_client)
        )
        if not built:
            return loaded

        options = dict(loaded.options or {})
        servers = dict(options.get("mcp_servers") or {})
        for name, config in built.servers.items():
            if name in servers:
                logger.warning(
                    "MCP server %r is already configured by the identity loader or the caller — "
                    "keeping theirs and skipping the agent's binding, rather than overwriting it.",
                    name,
                )
                continue
            servers[name] = config
        options["mcp_servers"] = servers

        # EXTEND an existing allowlist; never CREATE one. Creating a list where
        # none existed flips "every tool allowed" into "only these", silently
        # stripping Read/Write/Bash — measured, not theorised: workers built
        # that way reported a missing tool, made zero calls, and still came back
        # `completed`. (Note this list is inert under the permission mode this
        # run mode dispatches; the real gate is `policy_decider`. It matters the
        # moment a caller passes permission_mode="default".)
        if options.get("allowed_tools"):
            existing = list(options["allowed_tools"])
            options["allowed_tools"] = existing + [g for g in built.grants if g not in existing]

        return replace(loaded, options=options)

    async def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        # Fire the lifecycle session_ended once now that the conversation
        # is over. Sinks that count one ``agent_logs`` row per session
        # get exactly one row covering all turns.
        if self._lifecycle_started and self._lifecycle_session_id:
            with suppress(Exception):
                await self._emit_lifecycle_session_ended()
        if self._substrate_handle is not None:
            with suppress(Exception):
                await self._substrate_handle.close()
        with suppress(Exception):
            await self._pipeline.flush()

    async def _emit_lifecycle_session_ended(self) -> None:
        """Emit the single session_ended that closes this ComputerAgent's
        logical session. Carries cumulative usage and last assistant text
        so the AgentOS ``agent_logs`` row reflects the full conversation."""
        assert self._lifecycle_session_id is not None
        duration_ms = (time.monotonic() - self._lifecycle_started_at) * 1000.0
        agent_name = self._agent_name or (
            self._manifest.name if self._manifest is not None else None
        )
        if self._lifecycle_last_result_payload is not None:
            ev = TelemetryEvent.session_ended_ok(
                session_id=self._lifecycle_session_id,
                agent_name=agent_name,
                result=self._lifecycle_last_result_payload,
                duration_ms=duration_ms,
                last_assistant_text=self._lifecycle_last_assistant_text,
            )
            # That payload is the LAST TURN's result message, so its usage
            # describes one turn. This row describes the conversation, so the
            # summed lifecycle totals replace it — otherwise every multi-turn
            # run is billed as its final turn alone.
            ev.payload["usage"] = {
                "input_tokens": self._lifecycle_usage.input_tokens,
                "output_tokens": self._lifecycle_usage.output_tokens,
                "cache_creation_input_tokens": self._lifecycle_usage.cache_creation_input_tokens,
                "cache_read_input_tokens": self._lifecycle_usage.cache_read_input_tokens,
            }
            ev.payload["total_cost_usd"] = self._lifecycle_usage.cost_usd
        else:
            # Engine never yielded a ResultMessage (gitagent path) —
            # build a payload from the cumulative usage rollup instead.
            ev = TelemetryEvent(
                kind="session_ended",
                session_id=self._lifecycle_session_id,
                agent_name=agent_name,
                payload={
                    "is_error": False,
                    "subtype": "ok",
                    "result": self._lifecycle_last_assistant_text,
                    "duration_ms": duration_ms,
                    "num_turns": 0,
                    "total_cost_usd": self._lifecycle_usage.cost_usd,
                    "usage": {
                        "input_tokens": self._lifecycle_usage.input_tokens,
                        "output_tokens": self._lifecycle_usage.output_tokens,
                        "cache_creation_input_tokens": self._lifecycle_usage.cache_creation_input_tokens,
                        "cache_read_input_tokens": self._lifecycle_usage.cache_read_input_tokens,
                    },
                },
            )
        ev.agent_description = self._agent_description
        await self._emit(ev)

    # ── one chat turn ──────────────────────────────────────────────────────

    async def run_chat(
        self, message: Any
    ) -> tuple[
        _SessionState,
        AsyncIterator[EngineEvent],
    ]:
        """Boot if needed, start the engine, return the live event stream.

        Yields :class:`EngineEvent`s. The harness coordinator augments the
        stream with its own ``session_started`` and ``session_ended``
        events at the boundaries.
        """
        if self._turn_in_flight:
            raise HarnessError(
                "a chat turn is already in flight on this ComputerAgent. One instance is one "
                "conversation with one workspace, so concurrent turns would interleave into the "
                "same engine session — run concurrent work as separate ComputerAgent instances."
            )
        await self._ensure_booted()
        assert self._workspace_path is not None
        assert self._manifest is not None

        # Reuse the same id across every chat on this coordinator so a
        # multi-turn conversation lands as ONE session doc in Mongo (with
        # one user+assistant pair appended to sessions[].entries per turn)
        # instead of N orphan sessions.
        if self._lifecycle_session_id is None:
            self._lifecycle_session_id = new_session_id()
        session_id = self._lifecycle_session_id
        state = _SessionState(
            session_id=session_id,
            agent_name=self._agent_name or self._manifest.name,
            agent_description=self._agent_description,
            pipeline=self._pipeline,
        )

        # The session store is threaded into the engine's own options rather
        # than read here: the SDK mirrors transcript batches to it and
        # materialises prior entries itself before spawning the subprocess.
        # Replaying it from this side as well would duplicate the transcript.

        await _enqueue_user_message(state.user_queue, message)

        ctx = EngineContext(
            session_id=session_id,
            manifest=self._manifest,
            workspace_path=self._workspace_path,
            envs=self._envs,
            user_message_queue=_QueueAsyncIterable(state.user_queue),
            on_permission_request=self._build_permission_resolver(state),
            cancel_event=state.cancel_event,
            session_store=self._session_store,
            policy_decider=self._policy_decider,
            budget_max_usd=self._budget_max_usd,
            resume_session_id=self._engine_session_id,
            # Supervised orchestration only; `None` for every other agent, and
            # an engine that ignores the field behaves exactly as before.
            dispatch=self._build_dispatch_hook(state),
        )

        # session_started fires ONCE per ComputerAgent lifecycle so AgentOS
        # treats N chats as one conversation. It carries identity + the first
        # prompt for the AgentOS server's first-turn seeding of
        # sessions[].entries (the server-side ingest projection owns that write;
        # AgentOSHttpSink just forwards the event). Subsequent chats emit
        # user_message events so each new prompt appends to entries without
        # re-seeding. The OtelSink captures per-turn content via turn_started
        # (emitted below), not via session_started.
        self._turn_in_flight = True
        prompt_str = _stringify_prompt(message)
        is_first_chat = not self._lifecycle_started
        if is_first_chat:
            self._lifecycle_started = True
            self._lifecycle_first_prompt = prompt_str
            self._lifecycle_started_at = time.monotonic()
            await self._emit_session_started(state, ctx, prompt=prompt_str)
        elif prompt_str and prompt_str != "<streaming>":
            # The server's user_message projection unconditionally appends
            # — no first_open race. OtelSink ignores user_message events.
            user_ev = TelemetryEvent(
                kind="user_message",
                session_id=session_id,
                agent_name=state.agent_name,
                agent_description=state.agent_description,
                payload={"text": prompt_str},
            )
            await self._emit(user_ev)

        # turn_started fires on EVERY chat (incl. the first). OtelSink consumes
        # it to open a fresh invoke_agent root per turn (one trace per turn) and
        # close the prior turn; AgentOS sinks ignore it. session_started above
        # carries identity only.
        _opts = ctx.manifest.options or {}
        turn_model = (
            _opts.get("model") if isinstance(_opts, dict) else getattr(_opts, "model", None)
        )
        await self._emit(
            TelemetryEvent.turn_started(
                session_id=session_id,
                agent_name=state.agent_name,
                agent_description=state.agent_description,
                prompt=prompt_str,
                model=turn_model,
            )
        )

        return state, self._stream_engine_events(state, ctx)

    # ── event translation ──────────────────────────────────────────────────

    async def _stream_engine_events(
        self, state: _SessionState, ctx: EngineContext
    ) -> AsyncIterator[EngineEvent]:
        """Drive the engine, then any orchestration this agent declares.

        A plain agent takes exactly one pass through the loop and behaves
        identically to before. A ``mode: "roster"`` orchestrator takes a second:
        its first turn composes the roster (machine text), the fan-out runs, and
        the workers' reports are fed back for one closing turn that writes the
        answer the caller actually gets.

        DIVERGENCE FROM THE TS PORT, stated because it is visible to callers.
        The TS harness WITHHOLDS the roster text — it never reaches the client.
        This SDK streams events live, so by the time the harness knows the text
        was a roster it has already been yielded. What the run REPORTS is still
        the closing answer: the second turn overwrites ``last_assistant_text``
        and the terminal result, so ``ChatResult`` and the ``session_ended``
        telemetry carry the answer, not the roster. A caller rendering raw
        assistant text mid-stream can suppress it by watching for
        ``worker_wave_started``.
        """
        orchestration = self._orchestration()
        turn_index = 0
        try:
            while True:
                turn_index += 1
                async for ev in self._drive_engine(state, ctx):
                    yield ev

                follow_up: str | None = None
                # Roster mode fans out AFTER the orchestrator's turn, so the
                # run — and the workspace — stay alive until the workers finish.
                # Only the first turn composes a roster; the closing turn is an
                # ordinary answer and must never be parsed as one, or a wave
                # would follow every wave forever.
                if (
                    orchestration is not None
                    and getattr(orchestration, "mode", None) == "roster"
                    and turn_index == 1
                ):
                    async for wave_ev in self._run_wave(state, orchestration):
                        yield wave_ev
                    follow_up = self._pending_follow_up
                    self._pending_follow_up = None

                for wave_ev in _drain(state.wave_events):
                    yield wave_ev

                if not follow_up:
                    break
                ctx = await self._follow_up_context(state, ctx, follow_up)
        except BaseException as exc:
            duration_ms = (time.monotonic() - state.started_at) * 1000.0
            with suppress(Exception):
                err_ev = TelemetryEvent.session_ended_error(
                    session_id=state.session_id,
                    agent_name=state.agent_name,
                    exc=exc,
                    duration_ms=duration_ms,
                )
                err_ev.agent_description = state.agent_description
                await self._emit(err_ev)
            raise
        else:
            # Persist the latest assistant text + ResultMessage so dispose
            # can build a faithful session_ended payload with cumulative
            # data. NO per-chat session_ended emission — that would
            # produce N agent_logs rows for one conversation.
            if state.last_assistant_text:
                self._lifecycle_last_assistant_text = state.last_assistant_text
            if state.result_payload is not None:
                self._lifecycle_last_result_payload = state.result_payload
            # This turn is over and `state.usage` is final — fold it into the
            # conversation total that `session_ended` reports.
            self._lifecycle_usage.add(state.usage)
            # Drain pending sink tasks for THIS chat so the next chat's
            # user_message lands AFTER this chat's assistant_message in
            # ``sessions[].entries``. ``Pipeline.emit`` schedules sink
            # writes as background tasks; without flushing here the
            # next chat's events race and entries land out-of-order.
            self._turn_in_flight = False
            with suppress(Exception):
                await self._pipeline.flush()

    async def _drive_engine(
        self, state: _SessionState, ctx: EngineContext
    ) -> AsyncIterator[EngineEvent]:
        """Iterate the engine for ONE turn, translate each event, push telemetry.

        Called once per engine turn. A roster orchestrator takes a second turn
        (the closing synthesis), so the per-turn state that used to be locals
        here — the last assistant text, the terminal result payload — lives on
        ``state`` instead: the caller reads it after this generator ends, and
        the second turn is meant to overwrite it.
        """
        # Fired once per turn when the first streamed content chunk arrives →
        # time-to-first-token. Only the OtelSink consumes it.
        first_token_emitted = False
        async for ev in self._engine.start_session(ctx):
            if ev.kind == "sdk_message":
                # Remember the engine's own session id so the next turn can
                # resume this conversation. It appears on every message the
                # CLI emits; the first one we see is the one to keep.
                engine_sid = getattr(ev.payload, "session_id", None)
                if isinstance(engine_sid, str) and engine_sid:
                    self._engine_session_id = engine_sid
            if ev.kind == "usage_snapshot":
                state.usage.absorb(ev)
                # The LIFECYCLE total is summed from finished turns at the
                # bottom of this function, not from these snapshots: a
                # cumulative engine restarts its counters each turn, so
                # absorbing them here would leave the conversation billed
                # as its final turn alone.
                # Forward as a synthetic ResultMessage-shaped telemetry
                # event so the existing OTel sink picks it up.
                await self._emit(
                    TelemetryEvent(
                        kind="usage_snapshot",
                        session_id=state.session_id,
                        agent_name=state.agent_name,
                        agent_description=state.agent_description,
                        payload={
                            "input_tokens": ev.input_tokens,
                            "output_tokens": ev.output_tokens,
                            "cache_creation_input_tokens": ev.cache_creation_input_tokens,
                            "cache_read_input_tokens": ev.cache_read_input_tokens,
                            "total_cost_usd": ev.cost_usd or 0.0,
                            "cost_semantic": ev.cost_semantic or "delta",
                        },
                        raw=ev,
                    )
                )
            elif ev.kind == "sdk_message":
                state.messages.append(ev.payload)
                state.last_assistant_text = _maybe_assistant_text(
                    ev.payload, state.last_assistant_text
                )
                state.result_payload = _maybe_result_payload(ev.payload, state.result_payload)
                # First, try the claude-agent-sdk-shape translator —
                # for the Claude engine this yields tool_use /
                # assistant_message / tool_result / usage_snapshot
                # events directly.
                upstream_events = TelemetryEvent.from_message(
                    ev.payload,
                    session_id=state.session_id,
                    agent_name=state.agent_name,
                )
                if upstream_events:
                    for tev in upstream_events:
                        if state.agent_description is not None:
                            tev.agent_description = state.agent_description
                        await self._emit(tev)
                        # An assistant_message marks the end of one LLM call;
                        # re-arm so the NEXT call's first streamed token fires
                        # its own response_first_token (per-call TTFT).
                        if tev.kind == "assistant_message":
                            first_token_emitted = False
                elif upstream_events is None:
                    # from_message returned None → unrecognized type, not a
                    # claude-agent-sdk message (e.g. Anthropic raw response,
                    # OpenAI ChatCompletion, plain dict from gitagent).
                    # Synthesise so AgentOS / OTel sinks see the same picture.
                    for tev in _synthesise_telemetry(
                        ev.payload,
                        session_id=state.session_id,
                        agent_name=state.agent_name,
                        agent_description=state.agent_description,
                    ):
                        await self._emit(tev)
                # upstream_events == [] → recognised SDK type with nothing to
                # emit (StreamEvent, RateLimitEvent, …). Use the first
                # content-bearing StreamEvent of the turn as the
                # time-to-first-token signal (OtelSink-only).
                elif not first_token_emitted and _is_first_token_stream_event(ev.payload):
                    first_token_emitted = True
                    with suppress(Exception):
                        ft_ev = TelemetryEvent(
                            kind="response_first_token",
                            session_id=state.session_id,
                            agent_name=state.agent_name,
                        )
                        if state.agent_description is not None:
                            ft_ev.agent_description = state.agent_description
                        await self._emit(ft_ev)
            elif ev.kind == "permission_request":
                # Engines ask the harness; the harness routes to user
                # callbacks / policy decider. The Future returned by
                # ``ctx.on_permission_request`` is what unblocks.
                pass
            elif ev.kind in {"session_started", "session_ended"}:
                # Engines may emit these but the coordinator owns the
                # canonical pair — ignore engine-side duplicates.
                pass
            elif ev.kind in WAVE_EVENT_KINDS:
                # An engine has no reason to emit these — the harness does —
                # but tolerate it rather than killing a turn over an event
                # whose whole purpose is observability.
                pass
            else:
                raise HarnessProtocolError(f"unknown engine event kind: {ev.kind!r}")
            yield ev
            # A supervised fan-out runs inside the engine's own tool call, so
            # its wave events are produced while this loop is parked awaiting
            # the next engine event. Drain them here: the CLI is blocked on
            # the tool result anyway, so the first thing that arrives after
            # the fan-out carries them out.
            for wave_ev in _drain(state.wave_events):
                yield wave_ev

    # ── orchestration ──────────────────────────────────────────────────────

    def _orchestration(self) -> Any:
        """The agent's orchestration spec, or ``None`` for a plain agent.

        A coordinator that is itself a WORKER always answers ``None``, and the
        fan-out is single-level as a result. That mirrors the TS harness
        structurally rather than by accident: there, the fan-out branch lives in
        ``driveSession`` while a worker is started with ``runSession``, so the
        branch is simply never reached for a worker. Here one class owns both
        paths, so the distinction has to be a flag.

        Without it a referenced worker whose own repo declares ``orchestration:``
        would fan out again, and nothing bounds the recursion: each level clones
        a repo, opens worktrees and spends the caller's budget.
        """
        if not self._drive_orchestration:
            return None
        return getattr(self._manifest, "orchestration", None) if self._manifest else None

    def _orchestrator_ref(self) -> Any:
        """A snapshot of this session for the fan-out to spawn workers from."""
        from ..orchestration.spawn_child import OrchestratorRef

        assert self._workspace_path is not None
        assert self._lifecycle_session_id is not None
        return OrchestratorRef(
            session_id=self._lifecycle_session_id,
            engine=self._engine,
            engine_name=self._engine.name,
            engine_options=dict((self._manifest.options if self._manifest else None) or {}),
            envs=self._envs,
            workdir=self._workspace_path,
            agent_id=self._agent_id,
            agent_name=self._agent_name or (self._manifest.name if self._manifest else None),
            policy_decider=self._policy_decider,
            telemetry_pipeline=self._pipeline,
            session_store=self._session_store,
            project_key=self._project_key,
            orchestration=self._orchestration(),
            git_token=self._git_token,
        )

    def _new_wave_reporter(self, state: _SessionState) -> Any:
        self._wave_seq += 1
        return WaveReporter(
            state.wave_events.put_nowait, state.session_id, f"wave_{self._wave_seq}"
        )

    def _build_dispatch_hook(self, state: _SessionState) -> Any:
        """Surface A — the in-session ``dispatch_many`` the engine exposes.

        Returns ``None`` for anything but a supervised orchestrator, so an
        ordinary session hands the engine no hook and nothing changes.
        """
        spec = self._orchestration()
        if spec is None or getattr(spec, "mode", None) != "supervised":
            return None

        from ..orchestration.contract import DispatchResultItem
        from ..orchestration.parse_roster import RosterEntry
        from ..orchestration.roster_run import dispatch_roster_entries
        from ..orchestration.worktree_dispatch import dispatch_worktree_entries

        async def dispatch(items: Any) -> Any:
            entries = tuple(
                RosterEntry(
                    task=item.task,
                    id=item.id,
                    use=item.use,
                    role=item.role,
                    scope=item.scope,
                    model=item.model,
                    tools=item.tools,
                )
                for item in items
            )
            reporter = self._new_wave_reporter(state)
            parent = self._orchestrator_ref()
            result: Any
            if spec.isolation == "worktree":
                result = await dispatch_worktree_entries(parent, spec, entries, reporter=reporter)
            else:
                result = await dispatch_roster_entries(parent, spec, entries, reporter=reporter)
            logger.info(
                "orchestration.dispatched: session=%s mode=supervised:%s dispatched=%d",
                state.session_id,
                spec.isolation,
                len(result.workers),
            )
            return [
                DispatchResultItem(id=w.id, status=w.status, text=w.text, error=w.error)
                for w in result.workers
            ]

        return dispatch

    async def _run_wave(self, state: _SessionState, spec: Any) -> AsyncIterator[EngineEvent]:
        """Surface B — fan out the workers this turn\'s roster named, streaming
        the wave\'s events as they happen.

        Runs the fan-out as a task and drains the reporter\'s queue while it
        runs, so a caller watching the stream sees each worker start and finish
        instead of a silent gap followed by everything at once. Never raises: a
        fan-out failure is logged and the turn ends with the orchestrator\'s own
        text.
        """
        from ..orchestration.roster_run import (
            direct_answer_reason,
            dispatch_roster,
            gate_applies,
            no_reports_to_fold,
            synthesis_enabled,
            wave_needs_synthesis,
        )
        from ..orchestration.scorecard import build_scorecard
        from ..orchestration.worker_digest import (
            build_direct_answer_prompt,
            build_synthesis_prompt,
        )
        from ..orchestration.worktree_dispatch import dispatch_worktree_roster

        text = _result_text(state.result_payload) or state.last_assistant_text
        reporter = self._new_wave_reporter(state)
        parent = self._orchestrator_ref()
        worktree = spec.isolation == "worktree"

        async def run() -> Any:
            if worktree:
                return await dispatch_worktree_roster(parent, spec, text, reporter)
            return await dispatch_roster(parent, spec, text, reporter)

        task = asyncio.ensure_future(run())
        task.add_done_callback(lambda _t: state.wave_events.put_nowait(None))
        while True:
            ev = await state.wave_events.get()
            if ev is None:
                break
            yield ev
            await self._emit_wave_telemetry(state, ev)

        try:
            result = task.result()
        except Exception:  # noqa: BLE001 — a fan-out must not lose the turn
            logger.exception("orchestration: the fan-out failed; keeping the agent's own text")
            return

        logger.info(
            "orchestration.dispatched: session=%s mode=%s dispatched=%d %s",
            state.session_id,
            "worktree" if worktree else "clone",
            result.dispatched,
            (
                f"conflicts={len(result.conflicts)}"
                if worktree
                else f"unmatched={len(result.unmatched)}"
            ),
        )

        gate = None
        if worktree and spec.collect == "gate" and gate_applies(result.outcome):
            # Measurement + the gate: score the integrated run — unless the
            # agent deliberately fanned out to nobody, which is not a run to
            # score (see `gate_applies`).
            card = build_scorecard(
                parent.agent_name or "agent",
                workers=result.workers,
                conflicts=result.conflicts,
                integrated=result.integrated,
                aborted=result.aborted,
                budget=result.budget,
            )
            gate = card.gate
            logger.info(
                "orchestration.scorecard: session=%s verdict=%s reasons=%s integrated=%d",
                state.session_id,
                card.gate.verdict,
                list(card.gate.reasons),
                len(card.integrated),
            )
            # The gate verdict is the point of a `collect: "gate"` run, and it
            # used to exist only in a log line — the caller who asked for the
            # work never learned whether it passed.
            score_ev = EngineEvent(
                kind="orchestration_scorecard",
                payload={
                    "session_id": state.session_id,
                    "wave_id": reporter.wave_id,
                    "verdict": card.gate.verdict,
                    "scorecard": _as_plain(card),
                },
            )
            yield score_ev
            await self._emit_wave_telemetry(state, score_ev)

        if not wave_needs_synthesis(result, result.outcome):
            logger.info(
                "orchestration.synthesis_skipped: session=%s outcome=%s — no workers "
                "dispatched and the orchestrator answered in prose, so its own answer stands",
                state.session_id,
                result.outcome,
            )
            return
        if not synthesis_enabled():
            return

        self._pending_follow_up = (
            # Nothing ran and nothing failed to run: the ONLY reason a closing
            # turn exists is that the orchestrator's text was a roster, so
            # asking it to summarise a wave would be asking it to summarise
            # nothing.
            build_direct_answer_prompt(direct_answer_reason(result.outcome))
            if no_reports_to_fold(result)
            else build_synthesis_prompt(
                workers=result.workers,
                unmatched=getattr(result, "unmatched", ()) or (),
                integrated=getattr(result, "integrated", ()) or (),
                conflicts=getattr(result, "conflicts", None),
                aborted=result.aborted,
                budget=result.budget,
                gate=gate,
                collect=spec.collect,
            )
        )

    async def _emit_wave_telemetry(self, state: _SessionState, ev: EngineEvent) -> None:
        """Mirror one wave event onto the telemetry pipeline.

        Best-effort like every other emit here: a sink must never cost a wave.
        """
        with suppress(Exception):
            tev = TelemetryEvent(
                kind=ev.kind,  # type: ignore[arg-type]
                session_id=state.session_id,
                agent_name=state.agent_name,
                agent_description=state.agent_description,
                payload=dict(ev.payload or {}),
                raw=ev,
            )
            await self._emit(tev)

    async def _follow_up_context(
        self, state: _SessionState, ctx: EngineContext, prompt: str
    ) -> EngineContext:
        """A context for the closing turn, resuming the engine session the
        orchestrator just finished.

        A fresh queue, because the previous one was closed when its single
        message drained. No ``user_message`` telemetry: this prompt is harness
        machinery, not something the user typed, and projecting it would put the
        harness\'s own instructions into the conversation transcript.
        """
        from dataclasses import replace

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        await _enqueue_user_message(queue, prompt)
        state.user_queue = queue
        return replace(
            ctx,
            user_message_queue=_QueueAsyncIterable(queue),
            resume_session_id=self._engine_session_id,
        )

    # ── helpers ────────────────────────────────────────────────────────────

    async def _emit_session_started(
        self, state: _SessionState, ctx: EngineContext, *, prompt: str
    ) -> None:
        """Mirror the proxy's ``session_started`` shape so existing sinks
        recognise the event without changes."""

        class _OptionsView:
            def __init__(self, manifest_options: Any, workspace: Path) -> None:
                self._opts = dict(manifest_options or {})
                self._opts.setdefault("cwd", workspace)

            def __getattr__(self, name: str) -> Any:
                return self._opts.get(name)

        opts_view = _OptionsView(ctx.manifest.options, ctx.workspace_path)
        ev = TelemetryEvent.session_started(
            session_id=state.session_id,
            agent_name=state.agent_name,
            agent_description=state.agent_description,
            options=opts_view,
            prompt=prompt,
        )
        # Engine + agent identity → OTel gen_ai.provider.name /
        # computeragent.engine.name / gen_ai.agent.version / gen_ai.agent.id.
        # The proxy path omits these; the sink falls back to claude-agent-sdk.
        ev.payload["engine"] = self._engine.name
        manifest = ctx.manifest
        if getattr(manifest, "version", None):
            ev.payload["agent_version"] = manifest.version
        if getattr(manifest, "sha", None):
            ev.payload["agent_sha"] = manifest.sha
        # Signal harness-mode to the AgentOS server's ingest projection, which
        # then writes ``source.type = "library"`` (no server-runnable source →
        # the UI hides live chat). We set it for sources the AgentOS harness
        # can't reconstruct on its own (a git URL or a local path that only
        # exists on this machine). We deliberately do NOT set it for an INLINE
        # source: the server's ``inlineSourceFor`` rebuilds a runnable
        # ``agent.yaml`` + ``CLAUDE.md`` from this event's payload
        # (system_prompt / allowed_tools / max_turns / permission_mode / model),
        # so an inline agent stays live-chattable in the AgentOS UI via the TS
        # harness's inline loader. (Drop-in proxy callers never reach here.)
        is_inline = isinstance(self._source, dict) and self._source.get("type") == "inline"
        if not is_inline:
            ev.payload["harness_mode"] = True
        await self._emit(ev)

    async def _emit(self, ev: TelemetryEvent) -> None:
        """Single emit chokepoint — stamps the stable ``agent_id`` onto every
        event (so the AgentOS ingest can key the registry/joins on it) then
        hands off to the pipeline. ``TelemetryEvent`` is mutable by design."""
        if self._agent_id is not None:
            ev.agent_id = self._agent_id
        # Fork linkage — stamp the parent ids so a child's events nest under
        # the parent (None for a top-level agent → field stays None).
        if self._parent_session_id is not None:
            ev.parent_session_id = self._parent_session_id
        if self._parent_agent_id is not None:
            ev.parent_agent_id = self._parent_agent_id
        await self._pipeline.emit(ev)

    def _build_permission_resolver(
        self, state: _SessionState
    ) -> Callable[[PermissionRequest], Awaitable[PermissionDecision]]:
        """Compose user ``on_tool_call`` callback + ``policy_decider`` into
        a single async function the engine awaits."""

        async def _resolve(req: PermissionRequest) -> PermissionDecision:
            if self._policy_decider is not None:
                with suppress(Exception):
                    result = await self._policy_decider(
                        req.tool_name,
                        dict(req.input or {}),
                        {"session_id": state.session_id, "call_id": req.call_id},
                    )
                    if isinstance(result, dict):
                        return result
                    # The existing PolicyToolAuthorizer returns
                    # PermissionResultAllow|Deny — match by attribute.
                    behaviour = getattr(result, "behavior", None)
                    if behaviour in {"allow", "deny"}:
                        return {
                            "behavior": behaviour,
                            "message": getattr(result, "message", ""),
                            "updated_input": getattr(
                                result, "updated_input", dict(req.input or {})
                            ),
                        }
            if self._on_permission_request_user is not None:
                with suppress(Exception):
                    started_at = time.monotonic()
                    decision = await self._on_permission_request_user(req)
                    # Mirror PolicyToolAuthorizer: every gate decision emits a
                    # policy_decision event so the OTel sink can stamp the
                    # decision + reason onto the live execute_tool span.
                    # (The policy_decider path above emits its own.)
                    with suppress(Exception):
                        await self._emit(
                            TelemetryEvent(
                                kind="policy_decision",
                                session_id=state.session_id,
                                agent_name=state.agent_name,
                                payload={
                                    "engine": "on_tool_call",
                                    "decision": (
                                        "deny"
                                        if isinstance(decision, dict)
                                        and decision.get("behavior") == "deny"
                                        else "allow"
                                    ),
                                    "reason": (
                                        str(decision.get("message") or "")
                                        if isinstance(decision, dict)
                                        else ""
                                    ),
                                    "latency_ms": (time.monotonic() - started_at) * 1000.0,
                                    "principal_id": "",
                                    "principal_groups": [],
                                    "tool_name": req.tool_name,
                                    "call_id": req.call_id,
                                    "agent_name": state.agent_name or "",
                                    "session_id": state.session_id,
                                },
                            )
                        )
                    return decision
            # Default — allow with original input.
            return {"behavior": "allow", "updated_input": dict(req.input or {})}

        return _resolve


# ── small helpers ──────────────────────────────────────────────────────────


class _QueueAsyncIterable:
    """Adapter that exposes an :class:`asyncio.Queue` as an
    :class:`AsyncIterable` of user messages. ``None`` is the
    sentinel-end-of-stream."""

    def __init__(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        self._queue = queue

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


def _drain(queue: asyncio.Queue[Any]) -> list[Any]:
    """Everything already in ``queue``, without waiting for more.

    Used where the consumer cannot block — the engine loop is mid-turn — so the
    events ride out with the next thing that happens rather than holding it up.
    """
    out: list[Any] = []
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            return out
        if item is not None:
            out.append(item)


def _result_text(payload: Any) -> str:
    """The final text off a terminal result message, when there is one."""
    result = getattr(payload, "result", None)
    if result is None and isinstance(payload, dict):
        result = payload.get("result")
    return result if isinstance(result, str) else ""


def _as_plain(value: Any) -> Any:
    """A dataclass tree as plain dicts/lists, for an event payload a sink will
    serialise. Anything already plain passes through untouched."""
    import dataclasses

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _as_plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {k: _as_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_as_plain(v) for v in value]
    return value


def _stringify_prompt(message: Any) -> str:
    """Derive a string-prompt for the session_started / turn_started events.

    The OtelSink captures this as the turn's user input message. Multi-message /
    streaming inputs return ``"<streaming>"`` so the sink's skip-on-sentinel
    logic kicks in.
    """
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        return "<chat-input>"
    return "<streaming>"


async def _enqueue_user_message(
    queue: asyncio.Queue[dict[str, Any] | None],
    message: Any,
) -> None:
    """Normalise user input shapes into ``{"role":"user","content":...}``."""
    if isinstance(message, str):
        await queue.put({"role": "user", "content": message})
    elif isinstance(message, dict):
        if "role" not in message:
            await queue.put({"role": "user", "content": message})
        else:
            await queue.put(message)
    elif isinstance(message, list):
        for entry in message:
            if isinstance(entry, str):
                await queue.put({"role": "user", "content": entry})
            elif isinstance(entry, dict):
                await queue.put(entry if "role" in entry else {"role": "user", "content": entry})
    elif isinstance(message, AsyncIterable):
        async for entry in message:
            await _enqueue_user_message(queue, entry)
    else:
        raise TypeError(f"unsupported chat message type: {type(message).__name__}")
    # Single-shot mode — close the queue after one ChatInput is drained.
    # ``ComputerAgent.chat`` reopens for each call.
    await queue.put(None)


def _maybe_assistant_text(payload: Any, current: str) -> str:
    """Pull text from an upstream AssistantMessage if present, else keep current."""
    content = getattr(payload, "content", None)
    if not content:
        return current
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts) if parts else current


def _is_first_token_stream_event(payload: Any) -> bool:
    """True for a claude-agent-sdk ``StreamEvent`` carrying the first model
    output of a turn — a ``content_block_start`` / ``content_block_delta`` raw
    Anthropic stream event. Duck-typed (no hard import) so non-streaming engines
    (gitagent) never trip it. ``message_start`` is excluded: it opens the stream
    before any content, so it would understate time-to-first-token."""
    if type(payload).__name__ != "StreamEvent":
        return False
    event = getattr(payload, "event", None)
    etype = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
    return etype in ("content_block_start", "content_block_delta")


def _maybe_result_payload(payload: Any, current: Any) -> Any:
    """Track the latest ``ResultMessage``-shaped payload across the stream."""
    try:
        from claude_agent_sdk.types import ResultMessage  # type: ignore

        if isinstance(payload, ResultMessage):
            return payload
    except ImportError:
        pass
    return current


def _synthesise_telemetry(
    payload: Any,
    *,
    session_id: str,
    agent_name: str | None,
    agent_description: str | None,
) -> list[TelemetryEvent]:
    """Build the right :class:`TelemetryEvent`s for engines whose payloads
    aren't ``claude_agent_sdk`` types.

    Handles three shapes:

    * **Anthropic ``Message``** — ``.content`` is a list of blocks with
      ``.type ∈ {"text", "tool_use"}``.
    * **OpenAI ``ChatCompletionMessage``** — ``.content`` is a string and
      ``.tool_calls`` is a list of ``ChatCompletionMessageToolCall``.
    * **Plain dict** — emitted by the engine for tool-result envelopes
      (``{"role": "tool", "tool_call_id": ..., "content": ...}``) or as
      a generic catch-all.

    The function is best-effort — anything it can't pattern-match becomes
    a single ``assistant_message`` with empty text rather than raising.
    """
    events: list[TelemetryEvent] = []

    def _ev(kind: str, p: dict[str, Any]) -> TelemetryEvent:
        out = TelemetryEvent(
            kind=kind,  # type: ignore[arg-type]
            session_id=session_id,
            agent_name=agent_name,
            payload=p,
            raw=payload,
        )
        if agent_description is not None:
            out.agent_description = agent_description
        return out

    # ── plain dict ─────────────────────────────────────────────────────
    if isinstance(payload, dict):
        role = payload.get("role")
        if role == "tool":
            events.append(
                _ev(
                    "tool_result",
                    {
                        "tool_use_id": payload.get("tool_call_id")
                        or payload.get("tool_use_id", ""),
                        "is_error": bool(payload.get("is_error", False)),
                        "content": str(payload.get("content", "")),
                    },
                )
            )
            return events
        if role == "assistant":
            text = payload.get("content") or ""
            events.append(
                _ev("assistant_message", {"text": str(text), "model": payload.get("model")})
            )
            return events
        # Unknown dict shape — fall through to last-resort below.

    # ── Anthropic Message (list of typed content blocks) ───────────────
    content = getattr(payload, "content", None)
    blocks_iterable = isinstance(content, list) and content
    if blocks_iterable:
        text_parts: list[str] = []
        tool_uses: list[tuple[str, str, dict[str, Any]]] = []
        for block in content:
            btype = getattr(block, "type", None)
            if btype == "text":
                t = getattr(block, "text", None)
                if isinstance(t, str):
                    text_parts.append(t)
            elif btype == "tool_use":
                tool_uses.append(
                    (
                        str(getattr(block, "id", "") or ""),
                        str(getattr(block, "name", "") or ""),
                        dict(getattr(block, "input", {}) or {}),
                    )
                )
        for tu_id, tu_name, tu_input in tool_uses:
            events.append(
                _ev(
                    "tool_use",
                    {
                        "tool_use_id": tu_id,
                        "tool_name": tu_name,
                        "tool_input": tu_input,
                    },
                )
            )
        events.append(
            _ev(
                "assistant_message",
                {
                    "text": "".join(text_parts),
                    "model": getattr(payload, "model", None),
                    "block_count": len(content),
                },
            )
        )
        return events

    # ── OpenAI ChatCompletionMessage (str content + tool_calls list) ───
    if hasattr(payload, "content") or hasattr(payload, "tool_calls"):
        text = getattr(payload, "content", None)
        tool_calls = list(getattr(payload, "tool_calls", []) or [])
        for tc in tool_calls:
            fn = getattr(tc, "function", None)
            arg_str = getattr(fn, "arguments", "{}") if fn is not None else "{}"
            try:
                import json as _json

                tool_input = _json.loads(arg_str) if arg_str else {}
                if not isinstance(tool_input, dict):
                    tool_input = {}
            except (TypeError, ValueError):
                tool_input = {}
            events.append(
                _ev(
                    "tool_use",
                    {
                        "tool_use_id": getattr(tc, "id", "") or "",
                        "tool_name": (getattr(fn, "name", "") if fn is not None else "") or "",
                        "tool_input": tool_input,
                    },
                )
            )
        if isinstance(text, str) or tool_calls:
            events.append(
                _ev(
                    "assistant_message",
                    {
                        "text": text if isinstance(text, str) else "",
                        "model": getattr(payload, "model", None),
                    },
                )
            )
            return events

    # ── last resort ────────────────────────────────────────────────────
    events.append(_ev("assistant_message", {"text": str(payload)[:1000]}))
    return events


__all__ = ["HarnessCoordinator", "_SessionState"]
