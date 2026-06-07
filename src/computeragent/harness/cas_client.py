"""ComputerAgent-server (CAS) client — the richer `:8787` surface.

Targets ``examples/computeragent-server.ts``: `/run` (one-shot), warm
`/sandboxes` + `/sandboxes/:id/chat` (multi-turn), `/health` (substrate
discovery). Unlike the bare `/v1` protocol, the SSE stream arrives on the POST
response itself (no `/events`/`Last-Event-ID`), with extra framing events
``ca_done {usage}`` and ``ca_error {message}`` terminating a turn.

Reuses the shared ``HarnessEvent`` DTOs + :func:`parse_sse` from ``protocol.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any, cast

try:
    import httpx
except ImportError as _exc:  # pragma: no cover - guarded by lazy export
    raise ImportError(
        "The remote harness client requires the [remote] extra. "
        "Install with: pip install 'computer-agent-py[remote]'"
    ) from _exc

from .errors import HarnessProtocolError
from .protocol import HarnessEvent, _resolve_basic_auth, parse_event, parse_sse


class ComputerAgentServerClient:
    """Async client for the ComputerAgent server (`/run`, `/sandboxes`)."""

    def __init__(
        self,
        *,
        base_url: str,
        auth: tuple[str, str] | str | None = None,
        token: str | None = None,
        timeout: float | None = None,
        client: Any | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        headers = {"content-type": "application/json"}
        bearer = token or (auth if isinstance(auth, str) else None)
        if bearer:
            headers["authorization"] = f"Bearer {bearer}"
        basic = None if bearer else _resolve_basic_auth(auth)
        to = httpx.Timeout(timeout if timeout is not None else 30.0, read=None)
        self._client: Any = client or httpx.AsyncClient(timeout=to, headers=headers, auth=basic)
        self._owns_client = client is None

    # ── REST ──────────────────────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        """`GET /health` → `{ok, activeRuns, runtimes, defaultRuntime}` (substrate discovery)."""
        r = await self._client.get(f"{self._base}/health")
        await self._raise(r, "health")
        return cast("dict[str, Any]", r.json())

    async def create_sandbox(self, body: dict[str, Any]) -> dict[str, Any]:
        """`POST /sandboxes` → `{sandboxId, sessionId, reused?}`. Idempotent per `sessionId`."""
        r = await self._client.post(f"{self._base}/sandboxes", json=body)
        await self._raise(r, "create_sandbox")
        return cast("dict[str, Any]", r.json())

    async def delete_sandbox(self, sandbox_id: str, *, save: bool = True) -> None:
        params = {} if save else {"save": "false"}
        r = await self._client.delete(f"{self._base}/sandboxes/{sandbox_id}", params=params)
        # 404 on an already-reaped sandbox is fine during teardown.
        if r.status_code not in (200, 404):
            await self._raise(r, "delete_sandbox")

    async def heartbeat(self, sandbox_id: str) -> None:
        r = await self._client.post(f"{self._base}/sandboxes/{sandbox_id}/heartbeat")
        await self._raise(r, "heartbeat")

    # ── SSE (stream arrives on the POST) ──────────────────────────────────

    async def chat(self, sandbox_id: str, message: Any) -> AsyncIterator[HarnessEvent]:
        """`POST /sandboxes/{id}/chat` — one turn. Yields `ca_*` events incl. the
        terminal `ca_done {usage}` (or `ca_error`)."""
        async for ev in self._stream_post(
            f"{self._base}/sandboxes/{sandbox_id}/chat", {"message": message}, "chat"
        ):
            yield ev

    async def run(self, body: dict[str, Any]) -> AsyncIterator[HarnessEvent]:
        """`POST /run` — one-shot (auto-disposes server-side). Same SSE framing."""
        async for ev in self._stream_post(f"{self._base}/run", body, "run"):
            yield ev

    async def _stream_post(
        self, url: str, body: dict[str, Any], where: str
    ) -> AsyncIterator[HarnessEvent]:
        async with self._client.stream("POST", url, json=body) as r:
            await self._raise(r, where, streaming=True)
            async for _event_id, raw in parse_sse(r.aiter_bytes()):
                yield parse_event(raw)

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._owns_client:
            with suppress(Exception):
                await self._client.aclose()

    async def _raise(self, r: Any, where: str, *, streaming: bool = False) -> None:
        if r.status_code < 400:
            return
        body = ""
        with suppress(Exception):
            body = (await r.aread()).decode("utf-8", "replace") if streaming else r.text
        raise HarnessProtocolError(f"{where}: HTTP {r.status_code} from {self._base} — {body[:500]}")


__all__ = ["ComputerAgentServerClient"]
