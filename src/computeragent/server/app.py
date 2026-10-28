"""The app server: CAS's harness REST API in front of ComputerAgent.

Decision D22 in ``.claude/plans/p1-sdk-egress-routes.md``. Routes, bodies,
status codes and error codes follow the reference harness,
``computeragent-enterprise/packages/harness-server/src/routes/*``:

    POST /aws/lambda-microvms/runtime/v1/{hook}   Lambda MicroVM lifecycle hooks → 200
    GET  /v1/health
    POST /v1/sessions                             201 {sessionId, engine, identity, capabilities, eventsUrl}
    GET  /v1/sessions/{id}                        {sessionId, engine, identity, status}
    DELETE /v1/sessions/{id}                      {ok: true}
    POST /v1/sessions/{id}/messages               {message, credential?} → {ok: true}
    GET  /v1/sessions/{id}/events                 SSE (events.py), Last-Event-ID replay
    POST /v1/sessions/{id}/end-input | /cancel    {ok: true}
    POST /v1/sessions/{id}/permission/{callId}    {decision, input?, reason?} → {ok: true}
    GET|PUT|DELETE /v1/sessions/{id}/fs/file, GET …/fs/tree, POST …/fs/{edit,mkdir,move}
    GET  /ping, POST /invocations                  AgentCore: the same routes in relay envelope v1 (relay.py)

One ``ComputerAgent`` per session, built from the image's settings
(``COMPUTER_AGENT_*``) with no ``api_key=``. Old CAS passes settings in the
session's ``envs``: ``COMPUTER_AGENT_EGRESS_PROXY_SECRET`` (the sandbox token)
seeds the process environment; ``COMPUTER_AGENT_ID`` is the session's agent
(else the image's own); and each ``COMPUTER_AGENT_EGRESS_*_URL`` is used only
when the image's environment does not set it (an agent factory's own arguments
win over both). Every other ``envs`` key is ignored. Each message's
``credential`` replaces the token before its turn. The token is process-wide,
so turns run one at a time per process.
``engine`` is checked against the registered engines and the image's own
engine runs; ``identity`` is not read.
``messages`` are the session's first turns and ``streamingInput: false`` closes
its input after them, as in the reference. Resume across sandboxes is the
session store's (``sessionStore``, keyed by ``sessionId``).

Nothing here logs ``envs``, a ``credential``, a request header or a body.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import fs

if TYPE_CHECKING:
    from starlette.requests import Request

from .events import (
    EventLog,
    assistant_text,
    encode_sse,
    redaction_event,
    to_wire,
    tool_decision_event,
    usage_event,
)

logger = logging.getLogger("computeragent.server")

SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
KEEPALIVE_S = 20.0
HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1/"
ERROR_SOURCE = "COMPUTERAGENT_SERVER"

#: The ``envs`` keys old CAS passes at ``POST /v1/sessions``; the token is
#: also the variable the SDK reads it from.
TOKEN_ENV = "COMPUTER_AGENT_EGRESS_PROXY_SECRET"
AGENT_ID_ENV = "COMPUTER_AGENT_ID"
#: Egress routes CAS may pass in ``envs`` (``ComputerAgent`` argument names).
#: The image's own variables and the agent factory's arguments win over them.
SESSION_URL_SETTINGS = (
    "base_url",
    "llm_url",
    "mcp_url",
    "guardrails_url",
    "telemetry_url",
    "session_store_url",
)

#: Builds a session's agent: (session_id, options, session_store, on_tool_call) → ComputerAgent.
AgentFactory = Callable[..., Any]


def _sdk_version() -> str:
    try:
        return metadata.version("computer-agent-py")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def error(status: int, code: str, detail: str, **meta: Any) -> JSONResponse:
    """The harness error body: ``{detail, code, retry, severity, source, meta}``."""
    return JSONResponse(
        {
            "detail": detail,
            "code": code,
            "retry": status >= 500,
            "severity": "error",
            "source": ERROR_SOURCE,
            "meta": {"requestId": uuid.uuid4().hex, **meta},
        },
        status_code=status,
    )


def default_agent_factory(**kwargs: Any) -> Any:
    from ..harness import ComputerAgent

    on_tool_call = kwargs.pop("on_tool_call")
    # The Claude engine never asks for approvals (it runs under a
    # prompt-skipping permission mode); passing a callback only logs a warning.
    if os.environ.get("COMPUTER_AGENT_ENGINE") != "claude-agent-sdk":
        kwargs["on_tool_call"] = on_tool_call
    return ComputerAgent(**kwargs)


def _capabilities(engine: Any) -> dict[str, bool]:
    caps = getattr(engine, "capabilities", None)
    return {
        "streamingInput": bool(getattr(caps, "streaming_input", False)),
        "partialMessages": bool(getattr(caps, "partial_messages", False)),
        "permissionCallback": bool(getattr(caps, "permission_callback", False)),
        "sessions": bool(getattr(caps, "sessions", False)),
        "budget": bool(getattr(caps, "budget", False)),
    }


class _BusyClock:
    """Turns in flight, and when that last changed (AgentCore ``/ping``)."""

    def __init__(self) -> None:
        self.running = 0
        self.last_update = int(time.time())

    def change(self, delta: int) -> None:
        self.running += delta
        self.last_update = int(time.time())


@dataclass
class ServerSession:
    session_id: str
    engine: str
    agent: Any
    identity: dict[str, Any]
    capabilities: dict[str, bool]
    turn_lock: asyncio.Lock
    busy: _BusyClock
    log: EventLog = field(default_factory=EventLog)
    status: str = "pending"
    queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    pending: dict[str, tuple[asyncio.Future[Any], Any]] = field(default_factory=dict)
    worker: asyncio.Task[None] | None = None
    handle: Any = None
    turn_index: int = 0
    blocked: bool = False
    cancelled: bool = False

    async def emit(self, event: dict[str, Any]) -> None:
        await self.log.emit({**event, "sessionId": self.session_id})

    # ── telemetry from the agent ─────────────────────────────────────────

    async def observe(self, event: Any) -> None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.kind == "turn_started":
            prompt = payload.get("prompt")
            started: dict[str, Any] = {"kind": "ca_turn_started", "turnIndex": self.turn_index}
            if isinstance(prompt, str) and prompt != "<streaming>":
                started["message"] = {"role": "user", "content": prompt}
            self.turn_index += 1
            await self.emit(started)
        elif event.kind == "policy_decision":
            if payload.get("stage") == "llm_input":
                decision = redaction_event(self.session_id, payload)
                self.blocked = bool(decision["blocked"])
                await self.emit(decision)
            else:
                tool = tool_decision_event(self.session_id, payload)
                if tool is not None:
                    await self.emit(tool)

    # ── approvals ────────────────────────────────────────────────────────

    async def on_tool_call(self, request: Any) -> dict[str, Any]:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[request.call_id] = (future, request.input)
        event: dict[str, Any] = {
            "kind": "ca_permission_request",
            "callId": request.call_id,
            "toolName": request.tool_name,
            "input": request.input,
        }
        if getattr(request, "risk", None):
            event["risk"] = request.risk
        await self.emit(event)
        try:
            return dict(await future)
        finally:
            self.pending.pop(request.call_id, None)

    def resolve_permission(self, call_id: str, body: dict[str, Any]) -> bool:
        entry = self.pending.get(call_id)
        if entry is None or entry[0].done():
            return False
        future, original = entry
        if body["decision"] == "deny":
            future.set_result(
                {"behavior": "deny", "message": body.get("reason") or "denied by user"}
            )
        else:
            chosen = body.get("input", original)
            future.set_result({"behavior": "allow", "updated_input": chosen or {}})
        return True

    # ── turns ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self.worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            item = await self.queue.get()
            if item is None:
                break
            message, credential = item
            async with self.turn_lock:
                if credential:
                    os.environ[TOKEN_ENV] = credential
                if not await self._turn(message):
                    return
        await self.end("complete")

    async def _turn(self, message: Any) -> bool:
        """One turn; False when it ended the session."""
        self.status = "running"
        self.blocked = False
        self.busy.change(+1)
        held: dict[str, Any] | None = None
        saw_result = False
        last_text = ""
        try:
            self.handle = self.agent.chat(message)
            async for ev in self.handle:
                if ev.kind == "usage_snapshot":
                    await self.emit(usage_event(self.session_id, ev))
                    continue
                if ev.kind != "sdk_message":
                    continue
                if held is not None:
                    await self.emit({"kind": "sdk_message", "payload": held})
                    held = None
                payload = to_wire(ev.payload)
                if payload.get("type") == "result":
                    if self.blocked:
                        payload["ca_policy_blocked"] = True
                    # Sent after this turn's usage snapshot: the CAS client
                    # stops reading the turn at its result.
                    held, saw_result = payload, True
                    continue
                last_text = assistant_text(payload) or last_text
                await self.emit({"kind": "sdk_message", "payload": payload})
            if held is not None:
                await self.emit({"kind": "sdk_message", "payload": held})
            elif not saw_result and not self.cancelled:
                await self.emit(
                    {
                        "kind": "sdk_message",
                        "payload": {
                            "type": "result",
                            "subtype": "success",
                            "is_error": False,
                            "num_turns": 1,
                            "result": last_text,
                        },
                    }
                )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the engine failed: the session ends with the error
            if self.cancelled:
                return False
            logger.warning("session %s turn failed (%s)", self.session_id, type(exc).__name__)
            await self.end("error", exc)
            return False
        finally:
            self.handle = None
            self.busy.change(-1)
            if self.status == "running":
                self.status = "pending"

    async def end(self, reason: str, exc: BaseException | None = None) -> None:
        """Emit the one ``ca_session_ended`` and close the stream."""
        if self.log.closed:
            return
        self.status = {"complete": "completed", "cancelled": "cancelled"}.get(reason, "errored")
        event: dict[str, Any] = {"kind": "ca_session_ended", "reason": reason}
        if exc is not None:
            detail = f"{type(exc).__name__}: {exc}"[:2000]
            event["errorMessage"] = detail
            event["error"] = {
                "detail": detail,
                "code": getattr(exc, "code", None) or "ENGINE_FAILED",
                "retry": False,
                "severity": "error",
                "source": ERROR_SOURCE,
                "meta": {},
            }
        await self.emit(event)
        await self.log.close()

    async def cancel(self) -> None:
        self.cancelled = True
        for future, _ in list(self.pending.values()):
            if not future.done():
                future.set_result({"behavior": "deny", "message": "session cancelled"})
        if self.handle is not None:
            with contextlib.suppress(Exception):
                await self.handle.cancel()
        if self.worker is not None and not self.worker.done():
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.worker
        await self.end("cancelled")

    async def dispose(self) -> None:
        with contextlib.suppress(Exception):
            await self.agent.dispose()


class _Observer:
    """Pipeline middleware: hands each telemetry event to its session, inline,
    so ``ca_turn_started`` and ``ca_redaction_decision`` precede the turn's output."""

    def __init__(self, sessions: dict[str, ServerSession]) -> None:
        self._sessions = sessions

    async def __call__(self, event: Any) -> Any:
        session = self._sessions.get(getattr(event, "session_id", "") or "")
        if session is not None:
            with contextlib.suppress(Exception):
                await session.observe(event)
        return event


def _valid_message(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("role") != "user":
        return False
    content = value.get("content")
    return isinstance(content, str) or (
        isinstance(content, list)
        and all(isinstance(b, dict) and isinstance(b.get("type"), str) for b in content)
    )


async def _json_body(request: Request) -> Any:
    try:
        return await request.json()
    except ValueError:
        return None


def create_app(*, agent_factory: AgentFactory | None = None) -> Starlette:
    """The app server. ``agent_factory`` builds each session's agent (tests)."""
    from ..engines import registered_engines
    from ..telemetry import get_pipeline

    factory = agent_factory or default_agent_factory
    sessions: dict[str, ServerSession] = {}
    observer = _Observer(sessions)
    get_pipeline().add_middleware(observer)  # tool-gate decisions
    turn_lock = asyncio.Lock()
    busy = _BusyClock()
    version = _sdk_version()

    def session_or_404(request: Request) -> ServerSession | JSONResponse:
        sid = request.path_params["sid"]
        session = sessions.get(sid)
        if session is None:
            return error(404, "SESSION_NOT_FOUND", f"no session {sid}")
        return session

    async def lifecycle_hook(request: Request) -> Response:
        logger.info("lifecycle hook %s", request.path_params["hook"])
        return Response(status_code=200)

    async def health(_: Request) -> Response:
        engines = {name: _capabilities(e) for name, e in registered_engines().items()}
        return JSONResponse({"ok": True, "version": version, "engines": engines, "loaders": []})

    async def create_session(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return error(400, "REQUEST_JSON_INVALID", "the body is not a JSON object")
        issues = []
        if not isinstance(body.get("engine"), str) or not body["engine"]:
            issues.append("engine")
        if not isinstance(body.get("identity"), dict):
            issues.append("identity")
        envs = body.get("envs") or {}
        if not isinstance(envs, dict) or not all(isinstance(v, str) for v in envs.values()):
            issues.append("envs")
        messages = body.get("messages") or []
        if not isinstance(messages, list) or not all(_valid_message(m) for m in messages):
            issues.append("messages")
        options = body.get("options")
        if options is not None and not isinstance(options, dict):
            issues.append("options")
        if issues:
            return error(
                400, "REQUEST_BODY_INVALID", f"invalid fields: {', '.join(issues)}", fields=issues
            )
        engines = registered_engines()
        if body["engine"] not in engines:
            return error(
                400,
                "ENGINE_NAME_UNKNOWN",
                f"unknown engine {body['engine']}; available: {sorted(engines)}",
            )
        sid = body.get("sessionId") or f"sess_{uuid.uuid4().hex[:12]}"
        if not isinstance(sid, str) or sid in (".", "..") or not SESSION_ID.match(sid):
            return error(
                400,
                "SESSION_ID_INVALID",
                "sessionId must match [A-Za-z0-9._-]{1,128} and cannot be '.' or '..'",
            )
        if envs.get(TOKEN_ENV):
            os.environ[TOKEN_ENV] = envs[TOKEN_ENV]
        agent_kwargs: dict[str, Any] = {}
        if envs.get(AGENT_ID_ENV, "").strip():
            agent_kwargs["agent_id"] = envs[AGENT_ID_ENV].strip()
        from ..harness.settings import ENV_VARS

        for name in SESSION_URL_SETTINGS:
            value = envs.get(ENV_VARS[name], "").strip()
            if value and not os.environ.get(ENV_VARS[name], "").strip():
                agent_kwargs[name] = value
        old = sessions.pop(sid, None)
        if old is not None:
            await old.cancel()
            await old.dispose()

        holder: dict[str, ServerSession] = {}

        async def on_tool_call(req: Any) -> dict[str, Any]:
            return await holder["session"].on_tool_call(req)

        try:
            agent = await asyncio.to_thread(
                factory,
                session_id=sid,
                options=options,
                session_store=body.get("sessionStore"),
                on_tool_call=on_tool_call,
                **agent_kwargs,
            )
        except Exception as exc:
            from ..harness import AgentResolveError

            logger.warning("session %s: agent build failed (%s)", sid, type(exc).__name__)
            status = 502 if isinstance(exc, AgentResolveError) else 500
            return error(status, "AGENT_BUILD_FAILED", f"{type(exc).__name__}: {exc}"[:2000])
        engine = engines[body["engine"]]
        identity = {"name": getattr(agent.agent, "name", sid), "version": version}
        session = ServerSession(
            session_id=sid,
            engine=body["engine"],
            agent=agent,
            identity=identity,
            capabilities=_capabilities(engine),
            turn_lock=turn_lock,
            busy=busy,
        )
        holder["session"] = session
        agent.telemetry_pipeline.add_middleware(observer)
        sessions[sid] = session
        await session.emit(
            {
                "kind": "ca_session_started",
                "engine": body["engine"],
                "identity": identity,
                "capabilities": session.capabilities,
            }
        )
        session.start()
        initial_credential = envs.get(TOKEN_ENV)
        for message in messages:
            await session.queue.put((message, initial_credential))
        if not body.get("streamingInput"):
            await session.queue.put(None)
        logger.info("session %s created (engine %s)", sid, body["engine"])
        return JSONResponse(
            {
                "sessionId": sid,
                "engine": body["engine"],
                "identity": identity,
                "capabilities": session.capabilities,
                "eventsUrl": f"/v1/sessions/{sid}/events",
            },
            status_code=201,
        )

    async def get_session(request: Request) -> Response:
        s = session_or_404(request)
        if isinstance(s, JSONResponse):
            return s
        return JSONResponse(
            {
                "sessionId": s.session_id,
                "engine": s.engine,
                "identity": s.identity,
                "status": s.status,
            }
        )

    async def delete_session(request: Request) -> Response:
        s = session_or_404(request)
        if isinstance(s, JSONResponse):
            return s
        sessions.pop(s.session_id, None)
        await s.cancel()
        await s.dispose()
        return JSONResponse({"ok": True})

    async def push_message(request: Request) -> Response:
        s = session_or_404(request)
        if isinstance(s, JSONResponse):
            return s
        body = await _json_body(request)
        if not isinstance(body, dict):
            return error(400, "REQUEST_JSON_INVALID", "the body is not a JSON object")
        credential = body.get("credential")
        if not _valid_message(body.get("message")) or (
            credential is not None and not isinstance(credential, str)
        ):
            return error(400, "REQUEST_BODY_INVALID", "invalid fields: message", fields=["message"])
        if s.log.closed:
            return error(409, "SESSION_ENDED", f"session {s.session_id} has ended")
        await s.queue.put((body["message"], credential))
        return JSONResponse({"ok": True})

    async def events(request: Request) -> Response:
        s = session_or_404(request)
        if isinstance(s, JSONResponse):
            return s
        raw = request.headers.get("last-event-id") or request.query_params.get("lastEventId")
        try:
            since = int(raw) if raw is not None else -1
        except ValueError:
            since = -1

        async def stream() -> AsyncIterator[str]:
            async for item in s.log.iterate(since=since, keepalive=KEEPALIVE_S):
                yield ":keepalive\n\n" if item is None else encode_sse(*item)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"cache-control": "no-cache"}
        )

    async def end_input(request: Request) -> Response:
        s = session_or_404(request)
        if isinstance(s, JSONResponse):
            return s
        await s.queue.put(None)
        return JSONResponse({"ok": True})

    async def cancel(request: Request) -> Response:
        s = session_or_404(request)
        if isinstance(s, JSONResponse):
            return s
        logger.info("session %s cancel", s.session_id)
        await s.cancel()
        return JSONResponse({"ok": True})

    async def permission(request: Request) -> Response:
        s = session_or_404(request)
        if isinstance(s, JSONResponse):
            return s
        call_id = request.path_params["call_id"]
        body = await _json_body(request)
        if not isinstance(body, dict) or body.get("decision") not in ("allow", "deny", "modify"):
            return error(
                400, "REQUEST_BODY_INVALID", "invalid fields: decision", fields=["decision"]
            )
        if not s.resolve_permission(call_id, body):
            return error(404, "PERMISSION_CALL_UNKNOWN", f"no pending call {call_id}")
        decision: dict[str, Any] = {
            "kind": "ca_permission_decision",
            "callId": call_id,
            "decision": body["decision"],
            "source": "client",
        }
        if isinstance(body.get("reason"), str):
            decision["reason"] = body["reason"]
        await s.emit(decision)
        return JSONResponse({"ok": True})

    # ── files ────────────────────────────────────────────────────────────

    def fs_route(handler: Callable[[ServerSession, Path, Request], Any]) -> Callable[..., Any]:
        async def route(request: Request) -> Response:
            s = session_or_404(request)
            if isinstance(s, JSONResponse):
                return s
            try:
                return await handler(s, Path(s.agent.workspace), request)  # type: ignore[no-any-return]
            except fs.FsError as exc:
                return error(exc.status, exc.code, exc.detail)
            except OSError as exc:
                failure = fs.os_error(exc)
                return error(failure.status, failure.code, failure.detail)

        return route

    def need_path(request: Request) -> str | None:
        return request.query_params.get("path") or None

    async def fs_tree(_: ServerSession, ws: Path, request: Request) -> Response:
        try:
            depth = int(request.query_params.get("depth", "1"))
        except ValueError:
            depth = 1
        depth = 1 if depth < 0 else min(depth, 8)
        return JSONResponse(
            {"entries": fs.list_tree(ws, request.query_params.get("path") or "", depth)}
        )

    async def fs_read(_: ServerSession, ws: Path, request: Request) -> Response:
        path = need_path(request)
        if path is None:
            return error(400, "QUERY_PARAM_MISSING", "query parameter path is required")
        return Response(fs.read_bytes(ws, path), media_type="application/octet-stream")

    async def fs_write(_: ServerSession, ws: Path, request: Request) -> Response:
        path = need_path(request)
        if path is None:
            return error(400, "QUERY_PARAM_MISSING", "query parameter path is required")
        return JSONResponse({"ok": True, "size": fs.write_bytes(ws, path, await request.body())})

    async def fs_delete(_: ServerSession, ws: Path, request: Request) -> Response:
        path = need_path(request)
        if path is None:
            return error(400, "QUERY_PARAM_MISSING", "query parameter path is required")
        fs.remove_path(ws, path)
        return JSONResponse({"ok": True})

    async def fs_edit(_: ServerSession, ws: Path, request: Request) -> Response:
        body = await _json_body(request)
        if not (
            isinstance(body, dict)
            and isinstance(body.get("path"), str)
            and body["path"]
            and isinstance(body.get("oldString"), str)
            and isinstance(body.get("newString"), str)
        ):
            return error(400, "REQUEST_BODY_INVALID", "invalid fields: path, oldString, newString")
        n = fs.edit_file(
            ws, body["path"], body["oldString"], body["newString"], bool(body.get("replaceAll"))
        )
        return JSONResponse({"ok": True, "replacements": n})

    async def fs_mkdir(_: ServerSession, ws: Path, request: Request) -> Response:
        body = await _json_body(request)
        if not (isinstance(body, dict) and isinstance(body.get("path"), str) and body["path"]):
            return error(400, "REQUEST_BODY_INVALID", "invalid fields: path")
        fs.make_dir(ws, body["path"], bool(body.get("recursive")))
        return JSONResponse({"ok": True})

    async def fs_move(_: ServerSession, ws: Path, request: Request) -> Response:
        body = await _json_body(request)
        if not (
            isinstance(body, dict)
            and isinstance(body.get("from"), str)
            and body["from"]
            and isinstance(body.get("to"), str)
            and body["to"]
        ):
            return error(400, "REQUEST_BODY_INVALID", "invalid fields: from, to")
        fs.move_path(ws, body["from"], body["to"])
        return JSONResponse({"ok": True})

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        yield
        for s in list(sessions.values()):
            with contextlib.suppress(Exception):
                await s.cancel()
                await s.dispose()

    from .relay import relay_routes

    # AgentCore: the same routes inside relay envelope v1 (relay.py).
    ping, invocations = relay_routes(lambda: app, busy)

    app = Starlette(
        routes=[
            Route("/ping", ping, methods=["GET"]),
            Route("/invocations", invocations, methods=["POST"]),
            Route(HOOK_PREFIX + "{hook}", lifecycle_hook, methods=["POST", "GET"]),
            Route("/v1/health", health),
            Route("/v1/sessions", create_session, methods=["POST"]),
            Route("/v1/sessions/{sid}", get_session, methods=["GET"]),
            Route("/v1/sessions/{sid}", delete_session, methods=["DELETE"]),
            Route("/v1/sessions/{sid}/messages", push_message, methods=["POST"]),
            Route("/v1/sessions/{sid}/events", events, methods=["GET"]),
            Route("/v1/sessions/{sid}/end-input", end_input, methods=["POST"]),
            Route("/v1/sessions/{sid}/cancel", cancel, methods=["POST"]),
            Route("/v1/sessions/{sid}/permission/{call_id}", permission, methods=["POST"]),
            Route("/v1/sessions/{sid}/fs/tree", fs_route(fs_tree), methods=["GET"]),
            Route("/v1/sessions/{sid}/fs/file", fs_route(fs_read), methods=["GET"]),
            Route("/v1/sessions/{sid}/fs/file", fs_route(fs_write), methods=["PUT"]),
            Route("/v1/sessions/{sid}/fs/file", fs_route(fs_delete), methods=["DELETE"]),
            Route("/v1/sessions/{sid}/fs/edit", fs_route(fs_edit), methods=["POST"]),
            Route("/v1/sessions/{sid}/fs/mkdir", fs_route(fs_mkdir), methods=["POST"]),
            Route("/v1/sessions/{sid}/fs/move", fs_route(fs_move), methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    app.state.sessions = sessions
    app.state.busy = busy
    return app


__all__ = ["AgentFactory", "ServerSession", "create_app", "default_agent_factory", "error"]
