"""Fair-share spend enforcement for a wave of concurrent workers.

The number handed to each in-flight worker is the MINIMUM of its per-worker cap
and its fair share of what's left — because workers run concurrently: give N
in-flight workers the whole remaining budget each and the stage can overrun by
N× while every individual worker stays inside its own cap. Two layers: refuse an
under-funded wave BEFORE dispatch (a clean, cheap abort), and enforce the
per-worker cap IN-session via ``max_budget_usd``.

Single event loop ⇒ no locking needed (list append is atomic between awaits).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger("computeragent.orchestration.budget")

CostSource = Literal["sdk", "estimated", "stub"]

MIN_VIABLE_USD = 0.05
"""Below this there isn't enough left to be worth starting anything."""


class BudgetExceeded(Exception):
    """The wave is under-funded and was refused before any spend."""


@dataclass(frozen=True)
class Settlement:
    worker_id: str
    usd: float
    source: CostSource


@dataclass(frozen=True)
class BudgetSummary:
    total_usd: float
    spent_usd: float
    remaining_usd: float
    per_worker_cap_usd: float
    estimated_share: float
    settlements: tuple[Settlement, ...] = field(default_factory=tuple)


def _round(n: float, dp: int = 4) -> float:
    f = float(10**dp)
    return round(n * f) / f


class BudgetAllocator:
    def __init__(self, total_usd: float, per_worker_usd: float = 4.0) -> None:
        self.total_usd = total_usd
        self.per_worker_usd = per_worker_usd
        self._settlements: list[Settlement] = []

    @property
    def settlements(self) -> tuple[Settlement, ...]:
        return tuple(self._settlements)

    @property
    def spent(self) -> float:
        return sum(s.usd for s in self._settlements)

    def remaining(self) -> float:
        return max(0.0, self.total_usd - self.spent)

    def exhausted(self) -> bool:
        return self.remaining() < MIN_VIABLE_USD

    def slice(self, n: int) -> float:
        """The ``max_budget_usd`` to hand each worker in a wave of ``n``."""
        k = max(1, n)
        return min(self.per_worker_usd, self.remaining() / k)

    def check_before_dispatch(self, n: int) -> float:
        """Return the per-worker cap, or refuse the wave outright."""
        cap = self.slice(n)
        if cap < MIN_VIABLE_USD:
            raise BudgetExceeded(
                f"${self.remaining():.2f} left of ${self.total_usd:.2f} gives {n} worker(s) "
                f"${cap:.3f} each, below the ${MIN_VIABLE_USD:.2f} minimum. "
                "Aborting before dispatch."
            )
        return cap

    def record(self, worker_id: str, usd: float, source: CostSource = "sdk") -> None:
        """Settle a worker's spend. ``source`` distinguishes a measured cost
        from a guess."""
        self._settlements.append(Settlement(worker_id, max(0.0, usd), source))

    @property
    def estimated_share(self) -> float:
        """Fraction of the spend that is an estimate rather than an SDK figure."""
        total = self.spent
        if total <= 0:
            return 0.0
        return sum(s.usd for s in self._settlements if s.source != "sdk") / total

    def summary(self) -> BudgetSummary:
        return BudgetSummary(
            total_usd=_round(self.total_usd),
            spent_usd=_round(self.spent),
            remaining_usd=_round(self.remaining()),
            per_worker_cap_usd=_round(self.per_worker_usd),
            estimated_share=_round(self.estimated_share, 3),
            settlements=tuple(
                Settlement(s.worker_id, _round(s.usd), s.source) for s in self._settlements
            ),
        )


def warn_unenforceable_budget(
    capabilities: Any,
    *,
    engine: str,
    cap_usd: float,
    workers: int,
) -> None:
    """Say so when a fair-share cap was handed to workers whose engine cannot
    honour it.

    ``slice(n)`` rides on every worker as ``max_budget_usd``, but an engine that
    declares ``capabilities.budget is False`` (gitagent) simply ignores the
    field — so the only thing bounding that wave is ``check_before_dispatch``'s
    up-front refusal of an under-funded one, and a runaway worker can spend past
    its slice with nothing to stop it. The wave still runs exactly as before;
    this only makes a soft cap visible instead of letting it read as an enforced
    one.
    """
    if getattr(capabilities, "budget", None) is not False:
        return
    logger.warning(
        "orchestration.budget_unenforceable: engine %r declares capabilities.budget=False, so the "
        "per-worker max_budget_usd cap of $%.3f across %d worker(s) is NOT enforced in-session — "
        "the stage budget only gates whether the wave starts.",
        engine,
        cap_usd,
        workers,
    )


__all__ = [
    "MIN_VIABLE_USD",
    "BudgetAllocator",
    "BudgetExceeded",
    "BudgetSummary",
    "CostSource",
    "Settlement",
    "warn_unenforceable_budget",
]
