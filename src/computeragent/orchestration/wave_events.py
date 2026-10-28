"""Surfacing a fan-out on the ORCHESTRATOR's event stream.

Worker sessions have always been fully observable *after* the fact — their
telemetry is stamped with ``parent_session_id``, so it lands in the same places
any session's does. What was missing is the live view: dispatch was reported to
a logger, so a caller streaming the orchestrator's turn saw the orchestrator's
text, then a silent 30–120s gap while N workers did the actual work, then the
turn ending. Nothing on the stream explained the gap, and worker cost, failures,
merge conflicts and the gate verdict were invisible.

This is the emitter for the five events that close that gap. It is a thin shim
on purpose: it owns the wave id and the "never let telemetry break a wave"
guarantee, and nothing else.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..protocol_types import EngineEvent

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("computeragent.orchestration.wave")

WAVE_EVENT_KINDS = (
    "worker_wave_started",
    "worker_started",
    "worker_completed",
    "worker_wave_ended",
    "orchestration_scorecard",
)


class WaveReporter:
    """Frames each payload and pushes it onto one session's stream.

    Every emit is wrapped: a wave must never fail because a caller stopped
    consuming the stream mid-fan-out. A dropped event costs an affordance; a
    raised one would cost the wave — and the workers are the expensive part of
    the run.
    """

    def __init__(
        self,
        push: Callable[[EngineEvent], None],
        session_id: str,
        wave_id: str,
    ) -> None:
        self._push = push
        self.session_id = session_id
        #: ``wave_1``, ``wave_2``, … — unique within the orchestrator session.
        self.wave_id = wave_id

    def _emit(self, kind: str, info: dict[str, Any]) -> None:
        try:
            self._push(
                EngineEvent(
                    kind=kind,  # type: ignore[arg-type]
                    payload={
                        "session_id": self.session_id,
                        "wave_id": self.wave_id,
                        **{k: v for k, v in info.items() if v is not None},
                    },
                )
            )
        except Exception:  # noqa: BLE001 — a closed stream must not abort the fan-out
            logger.debug("wave reporter: dropped %s", kind, exc_info=True)

    def wave_started(self, **info: Any) -> None:
        self._emit("worker_wave_started", info)

    def worker_started(self, **info: Any) -> None:
        self._emit("worker_started", info)

    def worker_completed(self, **info: Any) -> None:
        self._emit("worker_completed", info)

    def wave_ended(self, **info: Any) -> None:
        self._emit("worker_wave_ended", info)

    def scorecard(self, **info: Any) -> None:
        self._emit("orchestration_scorecard", info)


__all__ = ["WAVE_EVENT_KINDS", "WaveReporter"]
