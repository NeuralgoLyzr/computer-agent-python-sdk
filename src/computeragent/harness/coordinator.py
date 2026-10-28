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


class _RefusalEngine:
    """Stands in for the engine on a turn whose message was blocked: the turn
    ends with the reply as its result, and the real engine never starts."""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def start_session(self, ctx: EngineContext) -> AsyncIterator[EngineEvent]:  # noqa: ARG002
        from claude_agent_sdk.types import ResultMessage

        yield EngineEvent(
            kind="sdk_message",
            payload=ResultMessage(
                subtype="success",
                duration_ms=0,
                duration_api_ms=0,
                is_error=False,
                num_turns=0,
                session_id="",
                stop_reason="end_turn",
                result=self._reply,
            ),
        )


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
        # MCP servers bound to the agent, and the `allowed_tools` grants for
        # them. Merged INTO what the loader resolved (a GAP agent's in-process
        # `gap_tools` server stays), before the caller overlay.
        mcp_servers: dict[str, Any] | None = None,
        mcp_allowed_tools: list[str] | None = None,
        # Runs at the start of every turn: resolves the turn's policies and
        # checks the user's message (see `guardrails.SrsTurnGuard`). A blocked
        # message ends the turn with its reply and the engine never runs.
        turn_guard: Any | None = None,
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
        self._mcp_servers: dict[str, Any] = dict(mcp_servers or {})
        self._mcp_allowed_tools: list[str] = list(mcp_allowed_tools or [])
        self._turn_guard = turn_guard

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

    # ── lifecycle ──────────────────────────────────────────────────────────

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
        self._substrate_handle = await self._substrate.boot(source=self._source, envs=self._envs)
        self._workspace_path = self._substrate_handle.workspace_path
        loaded = await self._identity_loader.load(
            source=self._source,
            target_engine=self._engine.name,
            workspace_path=self._workspace_path,
        )
        # Add the bound MCP servers to the loader's own, keeping both.
        if self._mcp_servers:
            from dataclasses import replace

            opts = dict(loaded.options or {})
            opts["mcp_servers"] = {**dict(opts.get("mcp_servers") or {}), **self._mcp_servers}
            if self._mcp_allowed_tools:
                allowed = list(opts.get("allowed_tools") or [])
                allowed.extend(t for t in self._mcp_allowed_tools if t not in allowed)
                opts["allowed_tools"] = allowed
            loaded = replace(loaded, options=opts)
        # Overlay caller-supplied options (from ComputerAgent kwargs) on
        # top of whatever the loader resolved. Caller intent wins.
        if self._options_overlay:
            merged_options = dict(loaded.options or {})
            merged_options.update(self._options_overlay)
            from dataclasses import replace

            loaded = replace(loaded, options=merged_options)
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
        self._manifest = loaded

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

        start = None
        if self._turn_guard is not None:
            start = await self._turn_guard.start_turn(message, session_id=session_id)
            message = start.message
        blocked_reply = start.blocked_reply if start is not None else None
        if blocked_reply is None:
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
        )

        # session_started fires ONCE per ComputerAgent lifecycle so AgentOS
        # treats N chats as one conversation. It carries identity + the first
        # prompt for the AgentOS server's first-turn seeding of
        # sessions[].entries (the server-side ingest projection owns that write;
        # the sink just forwards the event). Subsequent chats emit
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
        if start is not None and start.telemetry is not None:
            await self._emit(
                TelemetryEvent(
                    kind="policy_decision",
                    session_id=session_id,
                    agent_name=state.agent_name,
                    payload=dict(start.telemetry),
                )
            )

        engine = _RefusalEngine(blocked_reply) if blocked_reply is not None else None
        return state, self._stream_engine_events(state, ctx, engine=engine)

    # ── event translation ──────────────────────────────────────────────────

    async def _stream_engine_events(
        self, state: _SessionState, ctx: EngineContext, *, engine: Any | None = None
    ) -> AsyncIterator[EngineEvent]:
        """Iterate the engine, translate each event, push telemetry."""
        last_assistant_text = ""
        result_payload: Any = None
        # Fired once per turn when the first streamed content chunk arrives →
        # time-to-first-token. Only the OtelSink consumes it.
        first_token_emitted = False
        try:
            async for ev in (engine or self._engine).start_session(ctx):
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
                    last_assistant_text = _maybe_assistant_text(ev.payload, last_assistant_text)
                    result_payload = _maybe_result_payload(ev.payload, result_payload)
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
                else:
                    raise HarnessProtocolError(f"unknown engine event kind: {ev.kind!r}")
                yield ev
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
            if last_assistant_text:
                self._lifecycle_last_assistant_text = last_assistant_text
            if result_payload is not None:
                self._lifecycle_last_result_payload = result_payload
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
        # can't reconstruct on its own (a local path that only exists on this
        # machine). We deliberately do NOT set it for an INLINE
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
