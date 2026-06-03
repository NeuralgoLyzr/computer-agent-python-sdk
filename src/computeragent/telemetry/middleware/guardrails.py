"""``GuardrailFilter`` — generic transform/drop middleware.

Capabilities:

* truncate over-long string values in payload (``max_attribute_length``)
* drop tool-related events for tool names outside an allowlist (glob-aware)
* enforce a running per-session cost ceiling
* invoke a user-supplied async content filter

The cost ceiling is enforced *across events* by tracking per-session running
cost in a small in-memory map. The map shrinks on each ``session_ended`` so it
doesn't grow without bound.
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..event import TelemetryEvent

logger = logging.getLogger("computeragent.telemetry")

ContentFilter = Callable[["TelemetryEvent"], Awaitable["TelemetryEvent | None"]]


class GuardrailFilter:
    """Apply size, scope, and cost guardrails before events reach sinks."""

    def __init__(
        self,
        *,
        max_attribute_length: int | None = 8192,
        tool_name_allowlist: set[str] | None = None,
        cost_ceiling_usd: float | None = None,
        content_filter: ContentFilter | None = None,
    ) -> None:
        self.max_attribute_length = max_attribute_length
        self.tool_name_allowlist = tool_name_allowlist
        self.cost_ceiling_usd = cost_ceiling_usd
        self.content_filter = content_filter
        # session_id → running cost. Cleared on session_ended.
        self._cost_by_session: dict[str, float] = {}
        # Sessions that have already tripped the ceiling — drop further events
        # from them until session_ended.
        self._tripped: set[str] = set()

    async def __call__(self, event: TelemetryEvent) -> TelemetryEvent | None:
        # Remember the original kind so cleanup logic at the end can tell a
        # real session_ended apart from a synthetic one we minted here.
        original_kind = event.kind
        synthesized_session_ended = False

        # 1. Drop entirely if this session has already tripped the budget.
        if event.session_id in self._tripped and event.kind != "session_ended":
            return None

        # 2. Tool-name allowlist: only applies to tool_use / tool_result.
        if self.tool_name_allowlist is not None and event.kind in {"tool_use", "tool_result"}:
            name = event.payload.get("tool_name") or event.payload.get("tool_use_id") or ""
            if not _matches_any(name, self.tool_name_allowlist):
                return None

        # 3. Cost ceiling. usage_snapshot carries the running total cost.
        if self.cost_ceiling_usd is not None and event.kind == "usage_snapshot":
            cost = float(event.payload.get("total_cost_usd", 0.0) or 0.0)
            self._cost_by_session[event.session_id] = cost
            if cost > self.cost_ceiling_usd:
                self._tripped.add(event.session_id)
                # Transform this event into a synthetic session_ended so a
                # sink consumer still sees a terminal event, then forward it.
                event.kind = "session_ended"
                event.payload = {
                    "is_error": True,
                    "subtype": "budget_exceeded",
                    "total_cost_usd": cost,
                    "cost_ceiling_usd": self.cost_ceiling_usd,
                    "result": "",
                    "num_turns": 0,
                    "duration_ms": 0.0,
                    "usage": event.payload.get("usage", {}),
                }
                synthesized_session_ended = True

        # 4. Truncate long strings in payload (cheap; modifies in place).
        if self.max_attribute_length is not None:
            truncated = _truncate(event.payload, self.max_attribute_length)
            assert isinstance(truncated, dict)  # _truncate preserves shape for dict in → dict out
            event.payload = truncated

        # 5. User-supplied filter — last call so it sees the final payload.
        if self.content_filter is not None:
            try:
                filtered: TelemetryEvent | None = await self.content_filter(event)
                if filtered is None:
                    return None
                event = filtered
            except Exception:  # noqa: BLE001
                logger.warning("content_filter raised; dropping event", exc_info=True)
                return None

        # 6. session_ended bookkeeping — only on a REAL terminal event.
        # A synthetic session_ended we minted from the trip must keep
        # _tripped state set, so further events from that session are dropped.
        if original_kind == "session_ended" and not synthesized_session_ended:
            self._cost_by_session.pop(event.session_id, None)
            self._tripped.discard(event.session_id)

        return event


def _matches_any(name: str, allowlist: set[str]) -> bool:
    """Glob match — ``mcp__*`` and ``Read``-style literals both supported."""
    return any(fnmatch.fnmatchcase(name, pat) for pat in allowlist)


def _truncate(value: object, limit: int) -> object:
    if isinstance(value, dict):
        return {k: _truncate(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v, limit) for v in value]
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"…(+{len(value) - limit} chars)"
    return value
