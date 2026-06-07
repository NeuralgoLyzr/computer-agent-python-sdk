"""Remote-harness coordinator + transports.

When ``ComputerAgent(harness_url=...)`` is set, the agent doesn't run the engine
in-process — it talks to a remote harness server. :class:`RemoteCoordinator`
presents the same ``run_chat``/``dispose`` interface
:class:`computeragent.harness.ChatHandle` expects, but its stream yields raw
``HarnessEvent``s (``ca_*``) and its ``state`` accumulates messages/usage/session.

Two transports behind one coordinator:

* :class:`HarnessProtocolTransport` — bare ``/v1/sessions/*`` (port 7700). Full
  multi-turn + permission round-trip.
* :class:`ComputerAgentServerTransport` — ComputerAgent server ``/sandboxes`` +
  ``/run`` (port 8787). Client picks the substrate via ``runtime``; no permission
  HTTP endpoint (server-side ``permission_mode``/SRS only).
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any, Literal

try:
    import httpx
except ImportError as _exc:  # pragma: no cover - guarded by lazy export
    raise ImportError(
        "Remote harness support requires the [remote] extra. "
        "Install with: pip install 'computer-agent-py[remote]'"
    ) from _exc

from ..protocol_types import EngineEvent, IdentitySource, PermissionRequest
from ..telemetry import TelemetryEvent, get_pipeline, new_session_id
from ..telemetry.pipeline import Pipeline
from .cas_client import ComputerAgentServerClient
from .coordinator import _SessionState, _stringify_prompt
from .errors import HarnessProtocolError
from .protocol import (
    CaDone,
    CaError,
    CaPermissionRequest,
    CaSessionEnded,
    CaUsageSnapshot,
    HarnessClient,
    HarnessEvent,
    SdkMessage,
    user_message,
)
from .usage import UsageRollup

logger = logging.getLogger("computeragent.harness")

HarnessKind = Literal["protocol", "server", "auto"]
PermissionCallback = Callable[[PermissionRequest], Awaitable[dict[str, Any]]]


# ── transports ───────────────────────────────────────────────────────────────


class HarnessProtocolTransport:
    """Bare ``/v1/sessions/*`` transport. Supports the permission round-trip."""

    supports_permissions = True

    def __init__(
        self,
        *,
        client: HarnessClient,
        engine: str,
        loader: str,
        source: IdentitySource,
        envs: Mapping[str, str],
        options: Mapping[str, Any],
    ) -> None:
        self._client = client
        self._engine = engine
        self._loader = loader
        self._source = source
        self._envs = dict(envs)
        self._options = dict(options)
        self._session_id: str | None = None
        self._last_event_id: int | None = None

    async def ensure_open(self, lifecycle_session_id: str) -> str:
        if self._session_id is not None:
            return self._session_id
        body: dict[str, Any] = {
            "engine": self._engine,
            "identity": {"loader": self._loader, "source": self._source},
            "streamingInput": True,
            "sessionId": lifecycle_session_id,
        }
        if self._envs:
            body["envs"] = self._envs
        if self._options:
            body["options"] = self._options
        resp = await self._client.create_session(body)
        self._session_id = str(resp.get("sessionId") or lifecycle_session_id)
        return self._session_id

    async def run_turn(self, message: Any) -> AsyncIterator[HarnessEvent]:
        assert self._session_id is not None
        await self._client.send_message(self._session_id, user_message(message))
        # The /events SSE is long-lived under streamingInput=True — it spans every
        # turn, so a per-turn `ca_session_ended` never arrives. The turn ends when
        # the engine emits its terminal `result` SDK message (usage lands on the
        # wire just before it). Mirror the TS SDK's openTurnEventStream: yield each
        # event, and on the result terminator close the stream + synthesize a
        # `ca_session_ended` (the server session stays alive for the next turn).
        terminator: HarnessEvent | None = None
        stream = self._client.stream_events(self._session_id, self._last_event_id)
        try:
            async for event_id, ev in stream:
                if event_id is not None:
                    self._last_event_id = event_id
                if isinstance(ev, CaSessionEnded):
                    # Real session end (cancel / error / dispose) — pass through.
                    terminator = ev
                    break
                yield ev
                if _is_turn_result(ev):
                    terminator = CaSessionEnded(
                        kind="ca_session_ended", session_id=self._session_id, reason="complete"
                    )
                    break
        finally:
            # Close the long-lived SSE now (break leaves it open otherwise), so the
            # next turn re-opens cleanly with Last-Event-ID and dispose() won't race
            # a mid-chunk server close (→ httpx RemoteProtocolError).
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                with suppress(Exception):
                    await aclose()
        yield terminator or CaSessionEnded(
            kind="ca_session_ended",
            session_id=self._session_id,
            reason="error",
            error_message="SSE stream closed before turn completed",
        )

    async def decide_permission(
        self, call_id: str, decision: str, *, input: Any = None, reason: str | None = None
    ) -> None:
        assert self._session_id is not None
        await self._client.decide_permission(self._session_id, call_id, decision, input=input, reason=reason)

    async def dispose(self) -> None:
        if self._session_id is not None:
            with suppress(Exception):
                await self._client.delete_session(self._session_id)
        await self._client.aclose()


class ComputerAgentServerTransport:
    """ComputerAgent server transport — warm ``/sandboxes/:id/chat`` multi-turn.
    Substrate chosen via ``runtime``. No permission HTTP endpoint."""

    supports_permissions = False

    def __init__(
        self,
        *,
        client: ComputerAgentServerClient,
        engine: str,
        source: IdentitySource,
        runtime: str | None,
        envs: Mapping[str, str],
        options: Mapping[str, Any],
        model: str | None,
        agent_id: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        self._client = client
        self._engine = engine
        self._source = source
        self._runtime = runtime
        self._envs = dict(envs)
        self._options = dict(options)
        self._model = model
        self._agent_id = agent_id
        self._agent_name = agent_name
        self._sandbox_id: str | None = None

    async def ensure_open(self, lifecycle_session_id: str) -> str:
        if self._sandbox_id is not None:
            return self._sandbox_id
        body: dict[str, Any] = {
            "source": self._source,
            "harness": self._engine,
            "sessionId": lifecycle_session_id,
        }
        if self._runtime:
            body["runtime"] = self._runtime
        if self._envs:
            body["envs"] = self._envs
        if self._options:
            body["options"] = self._options
        if self._model:
            body["model"] = self._model
        # RBAC / dashboard identity — mirrors the AgentOS UI's sandboxBodyFor.
        # The CAS reads body.identity to (a) tag OTel spans (computeragent.agent.id)
        # and (b) write the chat_sessions index so the run shows in the dashboard
        # session list. groupId/ownerId are filled server-side from the cak_ key.
        if self._agent_id:
            body["identity"] = {"agentId": self._agent_id}
        if self._agent_name:
            body["agentName"] = self._agent_name
        resp = await self._client.create_sandbox(body)
        self._sandbox_id = str(resp["sandboxId"])
        return self._sandbox_id

    async def run_turn(self, message: Any) -> AsyncIterator[HarnessEvent]:
        assert self._sandbox_id is not None
        async for ev in self._client.chat(self._sandbox_id, message):
            yield ev

    async def decide_permission(self, call_id: str, decision: str, *, input: Any = None, reason: str | None = None) -> None:  # noqa: ARG002
        # CAS has no permission endpoint — server-side permission_mode/SRS only.
        return None

    async def dispose(self) -> None:
        if self._sandbox_id is not None:
            with suppress(Exception):
                await self._client.delete_sandbox(self._sandbox_id)
        await self._client.aclose()


# ── coordinator ──────────────────────────────────────────────────────────────


class RemoteCoordinator:
    """Drives a :class:`HarnessProtocolTransport` / :class:`ComputerAgentServerTransport`
    behind the ``ChatHandle`` interface. One per ``ComputerAgent`` instance."""

    def __init__(
        self,
        *,
        harness_url: str,
        harness_kind: str,
        engine: str,
        loader: str,
        source: IdentitySource,
        runtime: str | None,
        envs: Mapping[str, str],
        options: Mapping[str, Any],
        model: str | None,
        session_id: str | None,
        agent_name: str | None,
        agent_description: str | None,
        agent_id: str | None,
        on_tool_call: PermissionCallback | None,
        auth: tuple[str, str] | str | None,
        token: str | None,
        telemetry_pipeline: Pipeline | None,
    ) -> None:
        self._url = harness_url.rstrip("/")
        self._kind = harness_kind
        self._engine = engine
        self._loader = loader
        self._source = source
        self._runtime = runtime
        self._envs = dict(envs)
        self._options = dict(options)
        self._model = model
        self._agent_name = agent_name
        self._agent_description = agent_description
        # Stable agent identifier. Stamped onto every telemetry event in
        # _emit_safe AND sent in the CAS sandbox body (server transport) as
        # `identity.agentId` so the server tags spans + writes the session index.
        self._agent_id = agent_id
        self._on_tool_call = on_tool_call
        self._auth = auth
        self._token = token
        self._pipeline = telemetry_pipeline or get_pipeline()

        if on_tool_call is not None and harness_kind == "server":
            logger.warning(
                "on_tool_call is ignored with harness_kind='server' — the ComputerAgent "
                "server has no permission endpoint; use server-side permission_mode/SRS policy."
            )

        self._transport: HarnessProtocolTransport | ComputerAgentServerTransport | None = None
        self._lifecycle_session_id: str | None = session_id
        self._started = False
        self._started_at = 0.0
        self._lifecycle_usage = UsageRollup()
        self._lifecycle_last_assistant_text = ""
        self._disposed = False

    # ── transport selection ───────────────────────────────────────────────

    async def _ensure_transport(self) -> HarnessProtocolTransport | ComputerAgentServerTransport:
        if self._transport is not None:
            return self._transport
        kind = self._kind
        if kind == "auto":
            kind = await self._probe_kind()
        if kind == "server":
            self._transport = ComputerAgentServerTransport(
                client=ComputerAgentServerClient(base_url=self._url, auth=self._auth, token=self._token),
                engine=self._engine,
                source=self._source,
                runtime=self._runtime,
                envs=self._envs,
                options=self._options,
                model=self._model,
                agent_id=self._agent_id,
                agent_name=self._agent_name,
            )
        else:
            opts = dict(self._options)
            if self._model and "model" not in opts:
                opts["model"] = self._model
            self._transport = HarnessProtocolTransport(
                client=HarnessClient(base_url=self._url, auth=self._auth, token=self._token),
                engine=self._engine,
                loader=self._loader,
                source=self._source,
                envs=self._envs,
                options=opts,
            )
        return self._transport

    async def _probe_kind(self) -> Literal["protocol", "server"]:
        async with httpx.AsyncClient(timeout=5.0) as c:
            with suppress(Exception):
                r = await c.get(f"{self._url}/v1/health")
                if r.status_code == 200 and "engines" in r.json():
                    return "protocol"
            with suppress(Exception):
                r = await c.get(f"{self._url}/health")
                if r.status_code == 200 and "runtimes" in r.json():
                    return "server"
        return "protocol"

    # ── chat ────────────────────────────────────────────────────────────────

    async def run_chat(self, message: Any) -> tuple[_SessionState, AsyncIterator[HarnessEvent]]:
        if self._lifecycle_session_id is None:
            self._lifecycle_session_id = new_session_id()
        state = _SessionState(
            session_id=self._lifecycle_session_id,
            agent_name=self._agent_name or self._engine,
            agent_description=self._agent_description,
            pipeline=self._pipeline,
        )
        return state, self._stream(state, message)

    async def _stream(self, state: _SessionState, message: Any) -> AsyncIterator[HarnessEvent]:
        transport = await self._ensure_transport()
        prompt_str = _stringify_prompt(message)
        await transport.ensure_open(state.session_id)

        if not self._started:
            self._started = True
            self._started_at = time.monotonic()
            await self._emit_safe(self._session_started_event(state, prompt_str))
        elif prompt_str and prompt_str != "<streaming>":
            await self._emit_safe(
                TelemetryEvent(
                    kind="user_message",
                    session_id=state.session_id,
                    agent_name=state.agent_name,
                    agent_description=state.agent_description,
                    payload={"text": prompt_str},
                )
            )

        error: str | None = None
        try:
            async for ev in transport.run_turn(message):
                self._accumulate(state, ev)
                if (
                    isinstance(ev, CaPermissionRequest)
                    and transport.supports_permissions
                    and self._on_tool_call is not None
                ):
                    await self._handle_permission(transport, ev)
                await self._emit_for_event(state, ev)
                yield ev
                if isinstance(ev, CaSessionEnded):
                    if ev.reason in ("error", "budget_exceeded"):
                        error = ev.error_message or ev.reason
                    break
                if isinstance(ev, CaError):
                    error = ev.message or "remote harness error"
                    break
                if isinstance(ev, CaDone):
                    break
        except HarnessProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize transport faults
            raise HarnessProtocolError(f"remote harness transport error: {exc}") from exc
        finally:
            with suppress(Exception):
                await self._pipeline.flush()

        if error:
            raise HarnessProtocolError(error)

    # ── accumulation + permissions ─────────────────────────────────────────

    def _accumulate(self, state: _SessionState, ev: HarnessEvent) -> None:
        if isinstance(ev, SdkMessage):
            state.messages.append(ev.payload)
            role, text = _wire_text(ev.payload)
            if role == "assistant" and text:
                self._lifecycle_last_assistant_text = text
        elif isinstance(ev, CaUsageSnapshot):
            usage_ev = EngineEvent(
                kind="usage_snapshot",
                input_tokens=ev.input_tokens,
                output_tokens=ev.output_tokens,
                cache_creation_input_tokens=ev.cache_creation_input_tokens,
                cache_read_input_tokens=ev.cache_read_input_tokens,
                cost_usd=ev.cost_usd,
                cost_semantic=ev.cost_semantic,  # type: ignore[arg-type]  # wire str → Literal
            )
            state.usage.absorb(usage_ev)
            self._lifecycle_usage.absorb(usage_ev)

    async def _handle_permission(
        self,
        transport: HarnessProtocolTransport | ComputerAgentServerTransport,
        ev: CaPermissionRequest,
    ) -> None:
        assert self._on_tool_call is not None
        with suppress(Exception):
            decision = await self._on_tool_call(
                PermissionRequest(
                    call_id=ev.call_id,
                    tool_name=ev.tool_name,
                    input=ev.input,
                    risk=ev.risk,  # type: ignore[arg-type]  # wire str → Literal
                )
            )
            decision = decision or {}
            if decision.get("behavior") == "deny":
                await transport.decide_permission(ev.call_id, "deny", reason=decision.get("message"))
                return
            updated = decision.get("updated_input")
            if updated is not None and updated != ev.input:
                await transport.decide_permission(ev.call_id, "modify", input=updated)
            else:
                await transport.decide_permission(ev.call_id, "allow")

    # ── telemetry (best-effort; never breaks the chat) ─────────────────────

    def _session_started_event(self, state: _SessionState, prompt: str) -> TelemetryEvent:
        opts = dict(self._options)
        if self._model and "model" not in opts:
            opts["model"] = self._model
        return TelemetryEvent.session_started(
            session_id=state.session_id,
            agent_name=state.agent_name,
            agent_description=state.agent_description,
            options=_OptionsView(opts),
            prompt=prompt,
        )

    async def _emit_for_event(self, state: _SessionState, ev: HarnessEvent) -> None:
        tev: TelemetryEvent | None = None
        if isinstance(ev, CaUsageSnapshot):
            tev = TelemetryEvent(
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
                    "cost_semantic": ev.cost_semantic or "cumulative",
                },
            )
        elif isinstance(ev, SdkMessage):
            role, text = _wire_text(ev.payload)
            if role and text:
                tev = TelemetryEvent(
                    kind=f"{role}_message",  # type: ignore[arg-type]
                    session_id=state.session_id,
                    agent_name=state.agent_name,
                    agent_description=state.agent_description,
                    payload={"text": text},
                )
        if tev is not None:
            await self._emit_safe(tev)

    async def _emit_safe(self, ev: TelemetryEvent) -> None:
        # Stamp the stable agent_id onto every event (single chokepoint) so the
        # AgentOS ingest keys the registry/joins on it. TelemetryEvent is mutable.
        if self._agent_id is not None:
            ev.agent_id = self._agent_id
        with suppress(Exception):
            await self._pipeline.emit(ev)

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        if self._started and self._lifecycle_session_id:
            await self._emit_safe(self._lifecycle_session_ended_event())
        if self._transport is not None:
            with suppress(Exception):
                await self._transport.dispose()
        with suppress(Exception):
            await self._pipeline.flush()

    def _lifecycle_session_ended_event(self) -> TelemetryEvent:
        assert self._lifecycle_session_id is not None
        duration_ms = (time.monotonic() - self._started_at) * 1000.0
        return TelemetryEvent(
            kind="session_ended",
            session_id=self._lifecycle_session_id,
            agent_name=self._agent_name or self._engine,
            agent_description=self._agent_description,
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


# ── helpers ──────────────────────────────────────────────────────────────────


class _OptionsView:
    """Attribute-access view over an options dict (what TelemetryEvent.session_started reads)."""

    def __init__(self, opts: Mapping[str, Any]) -> None:
        self._opts = dict(opts)

    def __getattr__(self, name: str) -> Any:
        return self._opts.get(name)


def _is_turn_result(ev: HarnessEvent) -> bool:
    """Per-turn terminator: the engine's terminal SDK ``result`` message (or a
    ``system``/``session_end``). Mirrors ``isTurnResultEvent`` in
    packages/sdk/src/computer-agent.ts — under streamingInput the harness keeps
    the SSE open across turns, so this is the only end-of-turn signal."""
    if not isinstance(ev, SdkMessage) or not isinstance(ev.payload, dict):
        return False
    payload = ev.payload
    if payload.get("type") == "result":
        return True
    return payload.get("type") == "system" and payload.get("subtype") == "session_end"


def _wire_text(payload: Any) -> tuple[str | None, str | None]:
    """Best-effort (role, text) from a claude-agent-sdk SDKMessage wire dict.
    Returns (None, None) when no user-facing text is present (tool calls, etc.)."""
    if not isinstance(payload, dict):
        return (None, None)
    mtype = payload.get("type")
    if mtype == "result" and isinstance(payload.get("result"), str):
        return ("assistant", payload["result"])
    if mtype in ("assistant", "user"):
        msg = payload.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return (mtype, content)
        if isinstance(content, list):
            text = "".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
            return (mtype, text) if text else (None, None)
    return (None, None)


def build_remote_coordinator(
    *,
    harness_url: str,
    harness_kind: str,
    engine: str,
    loader: str,
    source: IdentitySource,
    runtime: str | None,
    envs: Mapping[str, str],
    options: Mapping[str, Any],
    model: str | None,
    session_id: str | None,
    agent_name: str | None,
    agent_description: str | None,
    agent_id: str | None = None,
    on_tool_call: PermissionCallback | None,
    auth: tuple[str, str] | str | None,
    token: str | None,
    telemetry_pipeline: Pipeline | None,
) -> RemoteCoordinator:
    return RemoteCoordinator(
        harness_url=harness_url,
        harness_kind=harness_kind,
        engine=engine,
        loader=loader,
        source=source,
        runtime=runtime,
        envs=envs,
        options=options,
        model=model,
        session_id=session_id,
        agent_name=agent_name,
        agent_description=agent_description,
        agent_id=agent_id,
        on_tool_call=on_tool_call,
        auth=auth,
        token=token,
        telemetry_pipeline=telemetry_pipeline,
    )


__all__ = [
    "ComputerAgentServerTransport",
    "HarnessProtocolTransport",
    "RemoteCoordinator",
    "build_remote_coordinator",
]
