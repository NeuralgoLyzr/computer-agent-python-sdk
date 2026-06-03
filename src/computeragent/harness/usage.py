"""Usage rollup — folds per-engine ``EngineEvent(kind="usage_snapshot")``
events into a single :class:`UsageRollup`.

The TS harness ``absorbUsage`` helper handles two distinct semantics:

* **cumulative** (claude-agent-sdk) — each ``ResultMessage`` reports totals
  for the whole session. The latest snapshot replaces older ones.
* **delta** (gitagent) — each chat-completion call reports just that call's
  tokens. The harness sums them across the session.

The Python port matches: a single rollup object tracks both modes per
field, and ``absorb`` picks the right operation from
``EngineEvent.cost_semantic``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..protocol_types import EngineEvent


@dataclass
class UsageRollup:
    """Running token + cost totals for one chat session.

    Fields stay at zero until at least one ``usage_snapshot`` arrives. After
    the first ``cost_semantic="cumulative"`` event lands, subsequent
    cumulative events overwrite; delta events still add on top (rare but
    possible if an engine yields both kinds).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0

    def absorb(self, event: EngineEvent) -> None:
        if event.kind != "usage_snapshot":
            return
        if event.cost_semantic == "cumulative":
            # Overwrite — cumulative totals supersede earlier readings.
            if event.input_tokens is not None:
                self.input_tokens = event.input_tokens
            if event.output_tokens is not None:
                self.output_tokens = event.output_tokens
            if event.cache_creation_input_tokens is not None:
                self.cache_creation_input_tokens = event.cache_creation_input_tokens
            if event.cache_read_input_tokens is not None:
                self.cache_read_input_tokens = event.cache_read_input_tokens
            if event.cost_usd is not None:
                self.cost_usd = event.cost_usd
        else:
            # delta (or unspecified — treat as delta to be additive-safe).
            self.input_tokens += event.input_tokens or 0
            self.output_tokens += event.output_tokens or 0
            self.cache_creation_input_tokens += event.cache_creation_input_tokens or 0
            self.cache_read_input_tokens += event.cache_read_input_tokens or 0
            self.cost_usd += event.cost_usd or 0.0


__all__ = ["UsageRollup"]
