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
    ``AGENTCORE_RELAY_REQUEST_INVALID``.

``instance`` is a random id minted on the first invocation, not at process
start (AgentCore may restore every microVM from one snapshot), so CAS reads a
changed ``instance`` as "AgentCore replaced the microVM".
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import re
import uuid
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse, Response, StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from starlette.requests import Request
    from starlette.types import ASGIApp, Message

RELAY_VERSION = 1
SSE_HEAD_PREFIX = ": ca-relay "
SKIP_RESPONSE_HEADERS = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
    "keep-alive",
}
_METHOD = re.compile(r"^[A-Z]+$")
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


def relay_routes(
    get_app: Callable[[], ASGIApp], busy: Any
) -> tuple[Callable[..., Any], Callable[..., Any]]:
    """The ``/ping`` and ``/invocations`` endpoints, relaying into ``get_app()``."""
    instance: dict[str, str] = {}

    def nonce() -> str:
        return instance.setdefault("id", str(uuid.uuid4()))

    def envelope(status: int, headers: dict[str, str], body: bytes) -> JSONResponse:
        return JSONResponse(
            {
                "relay": RELAY_VERSION,
                "instance": nonce(),
                "status": status,
                "headers": headers,
                **encode_body(body),
            }
        )

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
        method, path, headers, body = parsed
        call = _Call(get_app(), method, path, headers, body)
        try:
            status, raw_headers = await call.started
        except Exception:
            await call.close()
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
                try:
                    yield encode_sse_head(nonce(), status).encode()
                    async for chunk in call.stream():
                        yield chunk
                finally:
                    await call.close()

            return StreamingResponse(
                relayed(), media_type="text/event-stream", headers={"cache-control": "no-cache"}
            )
        data = b"".join([chunk async for chunk in call.stream()])
        await call.close()
        return envelope(status, out_headers, data)

    return ping, invocations


__all__ = [
    "RELAY_VERSION",
    "SSE_HEAD_PREFIX",
    "encode_body",
    "encode_sse_head",
    "parse_relay_request",
    "relay_routes",
]
