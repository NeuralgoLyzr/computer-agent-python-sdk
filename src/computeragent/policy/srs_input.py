"""The user's message through the guardrails check before the turn starts.

Each text block of the message is sent to ``POST {guardrails_url}/v1/check``
as one ``input`` check, in parallel; the agent gateway applies the org's and
the agent's policies. Folding follows CAS harness-server ``session.ts``
(``redactUserMessage`` / ``aggregateRedaction``):

* a ``rewrite`` replaces the block's text with the gateway's masked text;
* the results fold into one decision: the worst outcome wins
  (``error > blocked > redacted > clean``), and the reason and policy are the
  first a call reported;
* a block means the engine never runs and the reason is the reply;
* a call with no verdict blocks the message: the gateway owns fail-open.

A streamed (async-iterable) message is read entry by entry and every entry is
checked, as CAS checks each message ``pushUserMessage`` receives; the engine is
handed the checked, masked entries, so nothing unchecked reaches it.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import AsyncIterable
from dataclasses import dataclass
from typing import Any

from .guardrails_client import GUARDRAILS_TIMEOUT_S, GuardrailsUnavailable, check_text

logger = logging.getLogger("computeragent.policy.guardrails")

#: The reply when a block came with no reason of its own.
DEFAULT_BLOCK_REPLY = "Your message was blocked by the content policy."

_RANK = {"clean": 0, "redacted": 1, "blocked": 2, "error": 3}


def blocked_no_verdict_reply(reason: str) -> str:
    """The reply for a message refused because the check reached no verdict;
    reads the same as lyzr-agent's ``[Content blocked: RAI API request failed: …]``."""
    return f"[Content blocked: RAI policy check failed: {reason}]"


@dataclass(frozen=True)
class InputDecision:
    """The turn-level verdict on one user message."""

    message: Any
    outcome: str  # "clean" | "redacted" | "blocked" | "error"
    blocked: bool = False
    reason: str | None = None
    policy_id: str | None = None
    #: Whether any check was made; ``False`` means there was no text to check.
    checked: bool = False

    @property
    def reply(self) -> str:
        """What the user sees when the message is blocked."""
        return self.reason or DEFAULT_BLOCK_REPLY


@dataclass
class _Call:
    outcome: str
    blocked: bool
    reason: str | None
    policy_id: str | None


#: A user text inside a message: ``container[key]`` is the string.
_Slot = tuple[dict[str, Any], str]


def _text_slots(message: Any) -> list[_Slot]:
    """Every user text in a message, as (container, key) pairs into it."""
    slots: list[_Slot] = []

    def visit_content(holder: dict[str, Any]) -> None:
        content = holder.get("content")
        if isinstance(content, str):
            slots.append((holder, "content"))
        elif isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ):
                    slots.append((block, "text"))
        elif isinstance(content, dict):
            visit_content(content)

    if isinstance(message, dict):
        inner = message.get("message")
        visit_content(inner if isinstance(inner, dict) else message)
    elif isinstance(message, list):
        for entry in message:
            if isinstance(entry, dict):
                inner = entry.get("message")
                visit_content(inner if isinstance(inner, dict) else entry)
    return slots


async def check_user_message(
    message: Any,
    *,
    guardrails_url: str,
    api_key: str,
    timeout_s: float = GUARDRAILS_TIMEOUT_S,
    client: Any | None = None,
) -> InputDecision:
    """Check and mask one user message."""
    if isinstance(message, AsyncIterable):
        # The turn starts only after its input is read in full, so the entries
        # are collected here and checked like a list of messages.
        message = [entry async for entry in message]

    if isinstance(message, str):
        holder: dict[str, Any] = {"content": message}
        slots = _text_slots(holder)
    else:
        value = copy.deepcopy(message)
        if isinstance(value, list):
            # A bare string entry is a user message (as the coordinator
            # enqueues it), so it is checked like one.
            value = [
                {"role": "user", "content": entry} if isinstance(entry, str) else entry
                for entry in value
            ]
        holder = {"value": value}
        slots = _text_slots(holder["value"])
    slots = [(container, key) for container, key in slots if container[key]]
    if not slots:
        return InputDecision(message=message, outcome="clean")

    async def one(text: str) -> tuple[_Call, str | None]:
        try:
            verdict = await check_text(
                guardrails_url,
                api_key=api_key,
                text=text,
                timeout_s=timeout_s,
                client=client,
            )
        except GuardrailsUnavailable as exc:
            logger.warning("guardrails input check gave no verdict (%s); blocking", exc)
            no_verdict = _Call(
                outcome="error",
                blocked=True,
                reason=blocked_no_verdict_reply(str(exc)),
                policy_id=None,
            )
            return no_verdict, None
        call = _Call(
            outcome=verdict.outcome,
            blocked=verdict.blocked,
            reason=verdict.reason,
            policy_id=verdict.blocked_by_policy_id,
        )
        return call, verdict.text

    results = await asyncio.gather(*(one(container[key]) for container, key in slots))

    outcome = "clean"
    blocked = False
    reason: str | None = None
    policy_id: str | None = None
    for (container, key), (call, masked) in zip(slots, results, strict=True):
        if masked is not None:
            container[key] = masked
        if _RANK[call.outcome] > _RANK[outcome]:
            outcome = call.outcome
        blocked = blocked or call.blocked
        reason = reason or call.reason
        policy_id = policy_id or call.policy_id

    masked_message = holder["content"] if isinstance(message, str) else holder["value"]
    if blocked:
        logger.warning("guardrails blocked the user message (policy %s)", policy_id or "?")
    return InputDecision(
        message=masked_message,
        outcome=outcome,
        blocked=blocked,
        reason=reason,
        policy_id=policy_id,
        checked=True,
    )


__all__ = [
    "DEFAULT_BLOCK_REPLY",
    "InputDecision",
    "blocked_no_verdict_reply",
    "check_user_message",
]
