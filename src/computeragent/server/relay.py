"""AgentCore's front door on the app server: ``GET /ping`` and ``POST /invocations``.

Decision D24 in ``.claude/plans/p1-sdk-egress-routes.md``; port of
``computeragent-enterprise/packages/runtime-agentcore/src/invocations.ts`` and
``relay.ts`` (relay envelope v1).

* ``GET /ping`` → ``{"status": "Healthy" | "HealthyBusy", "time_of_last_update": <unix s>}``,
  ``HealthyBusy`` while any turn runs.
* ``POST /invocations`` carries ``{relay: 1, method, path, headers, body | bodyBase64}``
  (``path`` with its query; ``bodyBase64`` for bytes that are not UTF-8). The
  request is dispatched in-process to the app's own routes. The answer is
  always HTTP 200:

  - a successful server-sent-event answer is ``text/event-stream`` whose first
    frame is ``: ca-relay {"instance":…,"status":…}``, then the route's bytes
    as they are produced;
  - anything else is ``{relay: 1, instance, status, headers, body | bodyBase64}``;
  - a malformed envelope is that JSON with ``status: 400`` and code
    ``AGENTCORE_RELAY_REQUEST_INVALID``;
  - a JSON answer larger than ``RESPONSE_MAX_BYTES`` (AgentCore's payload cap)
    is replaced by that JSON with ``status: 502`` and code
    ``AGENTCORE_RESPONSE_TOO_LARGE``; event streams are not capped.

``instance`` is a random id minted on the first invocation, not at process
start (AgentCore may restore every microVM from one snapshot), so CAS reads a
changed ``instance`` as "AgentCore replaced the microVM".

Stream close (additive to v1). AgentCore never forwards a caller's disconnect,
so an envelope names its answer in ``headers["x-ca-relay-stream"]`` (a UUID v4,
stripped before dispatch). ``DELETE /_relay/streams/<id>`` is answered by the
relay itself: it closes that open answer as a dropped connection would
(relayed 204); an unknown id is a relayed 404.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse, Response, StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from starlette.requests import Request
    from starlette.types import ASGIApp, Message

logger = logging.getLogger("computeragent.server")

RELAY_VERSION = 1
SSE_HEAD_PREFIX = ": ca-relay "
#: The envelope header naming a relayed answer, so CAS can close it later.
RELAY_STREAM_HEADER = "x-ca-relay-stream"
RELAY_STREAM_CLOSE_PREFIX = "/_relay/streams/"
#: AgentCore's response payload cap (100 MiB).
RESPONSE_MAX_BYTES = 104_857_600
SKIP_RESPONSE_HEADERS = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
    "keep-alive",
}
_METHOD = re.compile(r"^[A-Z]+$")
_UUID_V4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE
)
#: The relay's own routes are never relayed.
_NOT_RELAYED = ("/invocations", "/ping")


def parse_relay_request(value: Any) -> tuple[str, str, dict[str, str], bytes | None] | None:
    """``(method, path, headers, body)`` of a valid v1 envelope, else ``None``."""
    if not isinstance(value, dict) or value.get("relay") != RELAY_VERSION:
        return None
    method, path, headers = value.get("method"), value.get("path"), value.get("headers")
    if not isinstance(method, str) or not _METHOD.match(method):
        return None
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
        return None
    body, body64 = value.get("body"), value.get("bodyBase64")
    if body is not None and not isinstance(body, str):
        return None
    if body64 is not None and not isinstance(body64, str):
        return None
    if isinstance(body64, str):
        try:
            return method, path, headers, base64.b64decode(body64, validate=True)
        except (binascii.Error, ValueError):
            return None
    return method, path, headers, body.encode("utf-8") if isinstance(body, str) else None


def encode_body(data: bytes) -> dict[str, str]:
    """UTF-8 bytes as ``body``, others as ``bodyBase64``; empty as nothing."""
    if not data:
        return {}
    try:
        return {"body": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"bodyBase64": base64.b64encode(data).decode("ascii")}


def encode_sse_head(instance: str, status: int) -> str:
    return f"{SSE_HEAD_PREFIX}{json.dumps({'instance': instance, 'status': status})}\n\n"


class _Call:
    """One in-process ASGI request whose answer is read as it is produced."""

    def __init__(
        self, app: ASGIApp, method: str, path: str, headers: dict[str, str], body: bytes | None
    ) -> None:
        raw_path, _, query = path.partition("?")
        self.scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": raw_path,
            "raw_path": raw_path.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": [
                (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
            ],
            "server": ("relay", 80),
            "client": ("relay", 0),
        }
        self._body = body if body is not None and method not in ("GET", "HEAD") else b""
        self._sent_body = False
        self._disconnect = asyncio.Event()
        self.started: asyncio.Future[tuple[int, list[tuple[bytes, bytes]]]] = (
            asyncio.get_running_loop().create_future()
        )
        self.chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.task = asyncio.create_task(self._run(app))

    async def _receive(self) -> Message:
        if not self._sent_body:
            self._sent_body = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            if not self.started.done():
                self.started.set_result((message["status"], list(message.get("headers", []))))
        elif message["type"] == "http.response.body":
            if message.get("body"):
                await self.chunks.put(message["body"])
            if not message.get("more_body", False):
                await self.chunks.put(None)

    async def _run(self, app: ASGIApp) -> None:
        try:
            await app(self.scope, self._receive, self._send)
        except Exception as exc:  # the route crashed: a 500 with no body
            if not self.started.done():
                self.started.set_exception(exc)
        finally:
            await self.chunks.put(None)

    async def stream(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self.chunks.get()
            if chunk is None:
                return
            yield chunk

    async def close(self) -> None:
        self._disconnect.set()
        if not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        if not self.started.done():  # closed before the app answered
            self.started.set_exception(ConnectionAbortedError("closed by the relay"))


def relay_routes(
    get_app: Callable[[], ASGIApp], busy: Any, *, max_response_bytes: int = RESPONSE_MAX_BYTES
) -> tuple[Callable[..., Any], Callable[..., Any]]:
    """The ``/ping`` and ``/invocations`` endpoints, relaying into ``get_app()``."""
    instance: dict[str, str] = {}
    #: Open calls by relay stream id (lower-case UUID v4).
    streams: dict[str, _Call] = {}

    def nonce() -> str:
        if "id" not in instance:
            instance["id"] = str(uuid.uuid4())
            logger.info("relay.instance id=%s", instance["id"])
        return instance["id"]

    def forget(sid: str | None, call: _Call, reason: str) -> None:
        if sid is not None and streams.get(sid) is call:
            del streams[sid]
            logger.info("relay.stream.closed stream=%s reason=%s", sid, reason)

    def render(status: int, headers: dict[str, str], body: bytes) -> bytes:
        # Serialized once, as JSONResponse would, so its size can be checked.
        return json.dumps(
            {
                "relay": RELAY_VERSION,
                "instance": nonce(),
                "status": status,
                "headers": headers,
                **encode_body(body),
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def envelope(status: int, headers: dict[str, str], body: bytes) -> Response:
        content = render(status, headers, body)
        if len(content) > max_response_bytes:
            # AgentCore would cut it mid-body; refuse it whole instead.
            error = {
                "detail": "The response is larger than AgentCore can carry.",
                "code": "AGENTCORE_RESPONSE_TOO_LARGE",
                "retry": False,
                "severity": "error",
                "source": "CAS_RUNTIME",
                "meta": {"maxBytes": max_response_bytes},
            }
            content = render(502, {"content-type": "application/json"}, json.dumps(error).encode())
        return Response(content, media_type="application/json")

    async def ping(_: Request) -> Response:
        return JSONResponse(
            {
                "status": "HealthyBusy" if busy.running > 0 else "Healthy",
                "time_of_last_update": busy.last_update,
            }
        )

    async def invocations(request: Request) -> Response:
        nonce()
        try:
            parsed = parse_relay_request(json.loads(await request.body()))
        except ValueError:
            parsed = None
        if parsed is None or parsed[1].partition("?")[0] in _NOT_RELAYED:
            error = {
                "detail": "The relay request is not a valid v1 envelope.",
                "code": "AGENTCORE_RELAY_REQUEST_INVALID",
                "retry": False,
                "severity": "error",
                "source": "COMPUTERAGENT_SERVER",
                "meta": {},
            }
            return envelope(400, {"content-type": "application/json"}, json.dumps(error).encode())
        method, path, envelope_headers, body = parsed
        # The stream id is the relay's, never the app's.
        headers: dict[str, str] = {}
        sid: str | None = None
        for name, text in envelope_headers.items():
            if name.lower() != RELAY_STREAM_HEADER:
                headers[name] = text
            elif _UUID_V4.match(text):
                sid = text.lower()
        route = path.partition("?")[0]
        if method == "DELETE" and route.startswith(RELAY_STREAM_CLOSE_PREFIX):
            target = route[len(RELAY_STREAM_CLOSE_PREFIX) :].lower()
            closing = streams.get(target) if _UUID_V4.match(target) else None
            if closing is None:
                return envelope(404, {}, b"")
            forget(target, closing, "deleted")
            await closing.close()
            return envelope(204, {}, b"")
        call = _Call(get_app(), method, path, headers, body)
        if sid is not None and sid not in streams:
            streams[sid] = call
            logger.info("relay.stream.opened stream=%s", sid)
            call.task.add_done_callback(lambda _: forget(sid, call, "ended"))
        else:
            sid = None

        async def finish() -> None:
            await call.close()
            forget(sid, call, "ended")

        try:
            status, raw_headers = await call.started
        except Exception:
            await finish()
            return envelope(500, {}, b"")
        out_headers: dict[str, str] = {}
        for key, value in raw_headers:
            name = key.decode("latin-1").lower()
            if name in SKIP_RESPONSE_HEADERS:
                continue
            text = value.decode("latin-1")
            out_headers[name] = f"{out_headers[name]}, {text}" if name in out_headers else text
        if 200 <= status < 300 and "text/event-stream" in out_headers.get("content-type", ""):

            async def relayed() -> AsyncIterator[bytes]:
                # A closed call (DELETE) queues its end marker, so this returns cleanly.
                try:
                    yield encode_sse_head(nonce(), status).encode()
                    async for chunk in call.stream():
                        yield chunk
                finally:
                    await finish()

            return StreamingResponse(
                relayed(), media_type="text/event-stream", headers={"cache-control": "no-cache"}
            )
        data = b"".join([chunk async for chunk in call.stream()])
        await finish()
        return envelope(status, out_headers, data)

    return ping, invocations


__all__ = [
    "RELAY_STREAM_HEADER",
    "RELAY_VERSION",
    "RESPONSE_MAX_BYTES",
    "SSE_HEAD_PREFIX",
    "encode_body",
    "encode_sse_head",
    "parse_relay_request",
    "relay_routes",
]
