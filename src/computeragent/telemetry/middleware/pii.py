"""``PiiRedactor`` — pattern-based redaction over ``event.payload``.

Built-in patterns cover the most common shapes we don't want shipping to a
remote observability backend by default: email, US phone, SSN, generic
credit-card-shaped digit runs, and AWS access key IDs / secret keys. The user
appends their own patterns via ``extra_patterns=`` and steers redaction by
path with ``path_allowlist=`` / ``path_denylist=``.

Fail-closed: a bad pattern or an exception inside the substitution drops the
event rather than forwarding possibly-unredacted content.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ..event import TelemetryEvent

logger = logging.getLogger("computeragent.telemetry")


# Conservative built-in patterns — high precision, low recall. Users add their
# own (badge IDs, internal account numbers, …) via extra_patterns.
BUILTIN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # email — RFC 5322 simplified
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    # US phone with optional country code; matches "+1 555-123-4567",
    # "(555) 123-4567", "555.123.4567", etc.
    re.compile(r"(?:\+?1[\s\-.])?\(?\d{3}\)?[\s\-.]\d{3}[\s\-.]\d{4}"),
    # US SSN
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # Credit card shaped — 13–19 digit runs with optional separators.
    re.compile(r"\b(?:\d[ \-]?){13,19}\b"),
    # AWS access key ID
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


Strategy = Literal["mask", "hash", "drop"]


class PiiRedactor:
    """Walks ``event.payload`` and redacts matches in string values.

    The walk descends into nested dicts and lists; tuples/sets are converted
    to lists during processing then put back. Non-string leaves are left
    alone. ``raw`` is never touched — sinks that need verbatim data can still
    grab it, but content-shipping sinks (OTel, AgentOS) only read ``payload``.
    """

    def __init__(
        self,
        *,
        strategy: Strategy = "mask",
        extra_patterns: list[re.Pattern[str] | str] | None = None,
        path_allowlist: list[str] | None = None,
        path_denylist: list[str] | None = None,
        include_builtins: bool = True,
    ) -> None:
        self.strategy = strategy
        self._patterns: list[re.Pattern[str]] = []
        if include_builtins:
            self._patterns.extend(BUILTIN_PATTERNS)
        for p in extra_patterns or []:
            self._patterns.append(p if isinstance(p, re.Pattern) else re.compile(p))
        self._allowlist = [p.strip(".") for p in (path_allowlist or [])]
        self._denylist = [p.strip(".") for p in (path_denylist or [])]

    async def __call__(self, event: TelemetryEvent) -> TelemetryEvent | None:
        try:
            event.payload = self._walk(event.payload, path="payload")
        except Exception:  # noqa: BLE001
            logger.warning("PiiRedactor failed; dropping event (fail-closed)", exc_info=True)
            return None
        return event

    # ── walk ----------------------------------------------------------------

    def _walk(self, value: Any, *, path: str) -> Any:
        if isinstance(value, dict):
            return {k: self._walk(v, path=f"{path}.{k}") for k, v in value.items()}
        if isinstance(value, list):
            return [self._walk(v, path=f"{path}[{i}]") for i, v in enumerate(value)]
        if isinstance(value, tuple):
            return tuple(self._walk(v, path=f"{path}[{i}]") for i, v in enumerate(value))
        if isinstance(value, str):
            return self._redact_if_applicable(value, path=path)
        return value

    # ── policy --------------------------------------------------------------

    def _redact_if_applicable(self, value: str, *, path: str) -> str:
        bare_path = path.removeprefix("payload.").removeprefix("payload")
        # Denylist wins outright — force redaction (or drop the field entirely
        # when strategy=drop, but at this layer we operate on the string value
        # so 'drop' on a string is equivalent to empty string).
        if any(_path_matches(bare_path, p) for p in self._denylist):
            return self._apply_strategy(value, force=True)
        # Allowlist short-circuits redaction — useful for whitelisted fields
        # like a known-safe URL or filename.
        if self._allowlist and any(_path_matches(bare_path, p) for p in self._allowlist):
            return value
        return self._apply_strategy(value, force=False)

    def _apply_strategy(self, value: str, *, force: bool) -> str:
        if force and self.strategy == "drop":
            return ""

        if self.strategy == "drop":
            # In drop mode, if any pattern matches anywhere, return empty.
            for pat in self._patterns:
                if pat.search(value):
                    return ""
            return value

        def replace(match: re.Match[str]) -> str:
            captured = match.group(0)
            if self.strategy == "hash":
                digest = hashlib.sha256(captured.encode("utf-8")).hexdigest()[:12]
                return f"<redacted:{digest}>"
            return "****"

        out = value
        for pat in self._patterns:
            try:
                out = pat.sub(replace, out)
            except re.error:
                logger.warning("regex sub failed for pattern %r; skipping", pat.pattern)
        # If the path is force-redacted but no pattern matched, return a
        # blanket replacement so the field's content never leaks.
        if force and out == value:
            if self.strategy == "hash":
                digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
                return f"<redacted:{digest}>"
            return "****"
        return out


def _path_matches(path: str, pattern: str) -> bool:
    """Naive glob-ish path matcher: supports literal segments and ``*``.

    Matches ``payload.foo.bar`` against ``foo.bar``, ``foo.*``, ``*.bar``.
    Not a full glob — intentionally simple so the rule semantics are obvious.
    """
    if pattern == "*":
        return True
    path_parts = path.split(".")
    pat_parts = pattern.split(".")
    if len(pat_parts) > len(path_parts):
        return False
    for pat, seg in zip(pat_parts, path_parts, strict=False):
        if pat != "*" and pat != seg:
            return False
    return True
