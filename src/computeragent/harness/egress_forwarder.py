"""A loopback forwarder between the Claude CLI and the egress proxy.

The CLI's environment and MCP headers are fixed when it is spawned, and its
answers are invisible to the SDK, so it could neither carry a reissued sandbox
token nor reveal one. Its model and MCP URLs on an egress origin are pointed
at a listener on ``127.0.0.1`` instead (path unchanged); each request is
replayed on the egress origin through an httpx client with the egress hooks,
so it is stamped with the newest token and its answer observed for a reissue.

* One listener per egress origin, started on first use, closed with
  :meth:`EgressForwarder.aclose`.
* HTTP/1.1 with ``h11``; bodies stream both ways without buffering (SSE,
  MCP streamable HTTP). Hop-by-hop headers are dropped.
* Forwards only to the origin its listener was opened for; a transport
  failure answers 502 with no body detail.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .egress_token import CREDENTIAL, EgressCredential, egress_event_hooks, origin_of

logger = logging.getLogger("computeragent.harness.egress_forwarder")

LOOPBACK = "127.0.0.1"
_READ_SIZE = 65536

#: Never forwarded in either direction (RFC 9110 §7.6.1), plus ``host``,
#: which the upstream request sets from its own URL.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)


def _end_to_end(headers: Any) -> list[tuple[bytes, bytes]]:
    named = set(HOP_BY_HOP)
    for name, value in headers:
        if name.lower() == b"connection":
            named.update(t.strip().lower() for t in value.decode("latin-1").split(","))
    return [(n, v) for n, v in headers if n.decode("latin-1").lower() not in named]


class EgressForwarder:
    """Loopback listeners that replay requests on their egress origins."""

    def __init__(
        self, *, credential: EgressCredential = CREDENTIAL, client: Any | None = None
    ) -> None:
        self._credential = credential
        self._client = client
        self._owns_client = client is None
        self._servers: dict[str, tuple[asyncio.base_events.Server, str]] = {}
        self._lock = asyncio.Lock()

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                event_hooks=egress_event_hooks(True),
                timeout=httpx.Timeout(10.0, read=None, write=None, pool=None),
            )
        return self._client

    async def url_for(self, url: str) -> str:
        """The loopback URL for an egress URL (path unchanged); any other URL as is."""
        upstream = origin_of(url)
        if upstream is None or not self._credential.is_egress(url):
            return url
        async with self._lock:
            entry = self._servers.get(upstream)
            if entry is None:
                server = await asyncio.start_server(
                    lambda r, w: self._serve(upstream, r, w), LOOPBACK, 0
                )
                port = server.sockets[0].getsockname()[1]
                entry = (server, f"http://{LOOPBACK}:{port}")
                self._servers[upstream] = entry
                logger.info("egress forwarder for %s listening on %s", upstream, entry[1])
        parts = urlsplit(url)
        local = urlsplit(entry[1])
        return urlunsplit((local.scheme, local.netloc, parts.path, parts.query, parts.fragment))

    async def rewrite(
        self, envs: Mapping[str, str], options: Mapping[str, Any] | None
    ) -> tuple[dict[str, str], dict[str, Any] | None]:
        """The Claude CLI's env and options with ``ANTHROPIC_BASE_URL`` and every
        ``mcp_servers[*].url`` on an egress origin pointed at the forwarder."""
        out_envs = dict(envs)
        base = out_envs.get("ANTHROPIC_BASE_URL")
        if base:
            out_envs["ANTHROPIC_BASE_URL"] = await self.url_for(base)
        if options is None:
            return out_envs, None
        out_options = dict(options)
        servers = out_options.get("mcp_servers")
        if isinstance(servers, Mapping):
            rewritten: dict[str, Any] = {}
            for name, server in servers.items():
                if isinstance(server, Mapping) and isinstance(server.get("url"), str):
                    server = {**server, "url": await self.url_for(server["url"])}
                rewritten[name] = server
            out_options["mcp_servers"] = rewritten
        return out_envs, out_options

    async def aclose(self) -> None:
        async with self._lock:
            servers = list(self._servers.values())
            self._servers.clear()
        for server, _ in servers:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        if self._owns_client and self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None

    # ── one connection ───────────────────────────────────────────────────

    async def _serve(
        self, upstream: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        import h11

        conn = h11.Connection(h11.SERVER)

        async def next_event() -> Any:
            while True:
                event = conn.next_event()
                if event is h11.NEED_DATA:
                    conn.receive_data(await reader.read(_READ_SIZE))
                    continue
                return event

        async def send(event: Any) -> None:
            data = conn.send(event)
            if data:
                writer.write(data)
                await writer.drain()

        try:
            while True:
                event = await next_event()
                if not isinstance(event, h11.Request):
                    break
                await self._one(upstream, event, conn, next_event, send)
                if conn.our_state is not h11.DONE or conn.their_state is not h11.DONE:
                    break
                conn.start_next_cycle()
        except Exception as exc:  # a broken client connection; nothing to answer
            logger.debug("egress forwarder connection ended (%s)", type(exc).__name__)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _one(
        self, upstream: str, request: Any, conn: Any, next_event: Any, send: Any
    ) -> None:
        import h11

        headers = _end_to_end(request.headers)
        names = {n.lower() for n, _ in request.headers}
        length = next((v for n, v in request.headers if n.lower() == b"content-length"), None)
        has_body = b"transfer-encoding" in names or (length is not None and length != b"0")

        async def body() -> AsyncIterator[bytes]:
            while True:
                event = await next_event()
                if isinstance(event, h11.Data):
                    yield bytes(event.data)
                else:
                    return

        if not has_body:
            await next_event()  # EndOfMessage

        client = self._http()
        url = upstream + request.target.decode("latin-1")
        outgoing = client.build_request(
            request.method.decode("ascii"),
            url,
            headers=[(n.decode("latin-1"), v.decode("latin-1")) for n, v in headers],
            content=body() if has_body else None,
        )
        try:
            response = await client.send(outgoing, stream=True)
        except Exception as exc:
            logger.warning("egress forwarder: %s unreachable (%s)", upstream, type(exc).__name__)
            if conn.their_state is not h11.DONE:
                # The rest of the request body is unread: the connection ends.
                await send(
                    h11.Response(
                        status_code=502,
                        headers=[(b"connection", b"close"), (b"content-length", b"0")],
                    )
                )
            else:
                await send(h11.Response(status_code=502, headers=[(b"content-length", b"0")]))
            await send(h11.EndOfMessage())
            return
        try:
            await send(
                h11.Response(
                    status_code=response.status_code,
                    headers=_end_to_end(response.headers.raw),
                    reason=response.reason_phrase.encode("latin-1")
                    if response.reason_phrase
                    else b"",
                )
            )
            if response.is_stream_consumed:  # already read by its transport
                if response.content:
                    await send(h11.Data(data=response.content))
            else:
                async for chunk in response.aiter_raw():
                    if chunk:
                        await send(h11.Data(data=chunk))
            await send(h11.EndOfMessage())
        finally:
            await response.aclose()


__all__ = ["HOP_BY_HOP", "LOOPBACK", "EgressForwarder"]
