"""The telemetry pipeline — middleware then fan-out to sinks.

Design properties:

* **Never breaks an agent run.** Every middleware and every sink is wrapped
  with broad exception handling; failures are logged and absorbed.
* **Fail-closed for PII.** A middleware that raises drops the event (rather
  than forwarding a possibly-unredacted one). Switchable via
  ``fail_open=True`` for debug builds.
* **Non-blocking sink emission.** Sinks run as background tasks so a slow
  exporter never stalls the message hot path. ``flush()`` waits for them at
  teardown.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from .event import TelemetryEvent

logger = logging.getLogger("computeragent.telemetry")


class TelemetryMiddleware(Protocol):
    """Transform or drop an event. Return None to drop."""

    async def __call__(self, event: TelemetryEvent) -> TelemetryEvent | None: ...


class TelemetrySink(Protocol):
    """Write an event to an external system."""

    async def emit(self, event: TelemetryEvent) -> None: ...

    async def flush(self, timeout: float = 5.0) -> None: ...

    async def aclose(self) -> None: ...


class Pipeline:
    """Owns the middleware chain and sink fan-out.

    A single ``Pipeline`` is normally enough per process; ``configure()`` builds
    the module-level default. Tests construct their own.
    """

    def __init__(
        self,
        *,
        middleware: list[TelemetryMiddleware] | None = None,
        sinks: list[TelemetrySink] | None = None,
        fail_open: bool = False,
    ) -> None:
        self._middleware: list[TelemetryMiddleware] = list(middleware or [])
        self._sinks: list[TelemetrySink] = list(sinks or [])
        self._fail_open = fail_open
        # Track scheduled sink tasks so flush() can await them.
        self._pending: weakref.WeakSet[asyncio.Task[None]] = weakref.WeakSet()

    # ── configuration -------------------------------------------------------

    def add_middleware(self, mw: TelemetryMiddleware) -> None:
        self._middleware.append(mw)

    def add_sink(self, sink: TelemetrySink) -> None:
        self._sinks.append(sink)

    def replace_middleware(self, mws: list[TelemetryMiddleware]) -> None:
        self._middleware = list(mws)

    def replace_sinks(self, sinks: list[TelemetrySink]) -> None:
        self._sinks = list(sinks)

    @property
    def middleware(self) -> list[TelemetryMiddleware]:
        return list(self._middleware)

    @property
    def sinks(self) -> list[TelemetrySink]:
        return list(self._sinks)

    # ── runtime -------------------------------------------------------------

    async def emit(self, event: TelemetryEvent) -> None:
        """Run middleware, then schedule sink emissions concurrently.

        Returns as soon as scheduling is done; sink writes happen in the
        background. Use :meth:`flush` to wait for them.
        """
        for mw in self._middleware:
            try:
                event_or_none = await mw(event)
            except Exception:  # noqa: BLE001 - middleware must never break the agent
                logger.warning(
                    "telemetry middleware raised; %s",
                    "forwarding event (fail_open=True)" if self._fail_open else "dropping event",
                    exc_info=True,
                )
                if not self._fail_open:
                    return
                event_or_none = event
            if event_or_none is None:
                return
            event = event_or_none

        for sink in self._sinks:
            task = asyncio.create_task(self._safe_emit(sink, event))
            self._pending.add(task)

    async def _safe_emit(self, sink: TelemetrySink, event: TelemetryEvent) -> None:
        try:
            await sink.emit(event)
        except Exception:  # noqa: BLE001 - sinks must never break the agent
            logger.warning("telemetry sink raised on emit (%s)", type(sink).__name__, exc_info=True)

    async def flush(self, timeout: float = 5.0) -> None:
        """Wait for outstanding sink writes, then flush each sink."""
        # Snapshot the pending tasks; new ones added during await are picked up
        # by sinks' own flush().
        tasks: list[asyncio.Task[None]] = [t for t in self._pending if not t.done()]
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
                )
            except TimeoutError:
                logger.warning(
                    "telemetry flush timed out after %.1fs; %d sink tasks still pending",
                    timeout,
                    len([t for t in tasks if not t.done()]),
                )

        for sink in self._sinks:
            try:
                await sink.flush(timeout=timeout)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "telemetry sink raised on flush (%s)", type(sink).__name__, exc_info=True
                )

    async def aclose(self) -> None:
        await self.flush()
        for sink in self._sinks:
            try:
                await sink.aclose()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "telemetry sink raised on aclose (%s)", type(sink).__name__, exc_info=True
                )


def _await(awaitable: Awaitable[None]) -> Awaitable[None]:
    """Type narrower for the linter (kept for future use)."""
    return awaitable
