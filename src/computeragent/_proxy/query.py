"""``query`` — drop-in for :func:`claude_agent_sdk.query` with a telemetry tap.

Every message yielded by the upstream stream is also reported into the active
telemetry pipeline. The user-visible stream is preserved verbatim — the proxy
never modifies or reorders yielded messages.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import claude_agent_sdk
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

from ..telemetry import TelemetryEvent, get_pipeline, new_session_id

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, AsyncIterator

logger = logging.getLogger("computeragent")


async def query(  # noqa: PLR0912 - mirror upstream's tolerant param surface
    *,
    prompt: str | AsyncIterable[dict[str, Any]],
    options: Any = None,
    transport: Any = None,
) -> AsyncIterator[Any]:
    """Drop-in for ``claude_agent_sdk.query`` — see upstream docs for the
    parameter semantics. The wrapper additionally pushes a
    :class:`~computeragent.telemetry.TelemetryEvent` per message into the
    active pipeline.

    The pipeline is always the module-level default unless the caller
    pre-configured one via :func:`computeragent.telemetry.configure`.
    """
    pipeline = get_pipeline()
    session_id = new_session_id()
    agent_name = _derive_agent_name(options)
    agent_description = _derive_agent_description(options)
    started_at = time.monotonic()
    last_assistant_text = ""

    try:
        await pipeline.emit(
            TelemetryEvent.session_started(
                session_id=session_id,
                agent_name=agent_name,
                agent_description=agent_description,
                options=options,
                prompt=prompt,
            )
        )

        last_result: ResultMessage | None = None
        upstream_kwargs: dict[str, Any] = {"prompt": prompt}
        if options is not None:
            upstream_kwargs["options"] = options
        if transport is not None:
            upstream_kwargs["transport"] = transport

        upstream_session_id: str | None = None
        async for msg in claude_agent_sdk.query(**upstream_kwargs):
            # Capture upstream's session id once for join-by-id later, but
            # do NOT rebind our `session_id` mid-stream — the sinks already
            # opened bookkeeping under the placeholder on session_started,
            # so swapping the key here would orphan that state and produce
            # empty agent_logs.query / missing sessions.entries[].
            real_sid = getattr(msg, "session_id", None)
            if (
                isinstance(real_sid, str)
                and real_sid
                and real_sid != session_id
                and upstream_session_id is None
            ):
                upstream_session_id = real_sid

            for ev in TelemetryEvent.from_message(
                msg, session_id=session_id, agent_name=agent_name
            ):
                # Carry the description forward on every event so sinks that
                # group by description (or want to surface it on per-message
                # telemetry) don't have to back-reference session_started.
                if agent_description is not None:
                    ev.agent_description = agent_description
                await pipeline.emit(ev)

            if isinstance(msg, AssistantMessage):
                # Track latest assistant text as a fallback when ResultMessage.result is empty.
                for block in getattr(msg, "content", []) or []:
                    if isinstance(block, TextBlock):
                        last_assistant_text = block.text

            if isinstance(msg, ResultMessage):
                last_result = msg

            yield msg

        duration_ms = (time.monotonic() - started_at) * 1000.0
        if last_result is not None:
            ev = TelemetryEvent.session_ended_ok(
                session_id=session_id,
                agent_name=agent_name,
                result=last_result,
                duration_ms=duration_ms,
                last_assistant_text=last_assistant_text,
            )
            ev.agent_description = agent_description
            if upstream_session_id is not None:
                ev.payload["upstream_session_id"] = upstream_session_id
            await pipeline.emit(ev)
        else:
            # Upstream finished without a ResultMessage — synthesize one event.
            await pipeline.emit(
                TelemetryEvent(
                    kind="session_ended",
                    session_id=session_id,
                    agent_name=agent_name,
                    agent_description=agent_description,
                    payload={
                        "is_error": False,
                        "subtype": "no_result",
                        "result": last_assistant_text,
                        "duration_ms": duration_ms,
                        "num_turns": 0,
                        "total_cost_usd": 0.0,
                        "usage": {},
                    },
                )
            )
    except BaseException as exc:
        duration_ms = (time.monotonic() - started_at) * 1000.0
        try:
            err_ev = TelemetryEvent.session_ended_error(
                session_id=session_id,
                agent_name=agent_name,
                exc=exc,
                duration_ms=duration_ms,
            )
            err_ev.agent_description = agent_description
            await pipeline.emit(err_ev)
        except Exception:  # noqa: BLE001
            logger.debug("error-path telemetry emit failed", exc_info=True)
        raise
    finally:
        try:
            await pipeline.flush()
        except Exception:  # noqa: BLE001
            logger.debug("pipeline flush in query() finally failed", exc_info=True)


def _derive_agent_name(options: Any) -> str | None:
    """Pick a stable agent name from options.

    Strategy:
      1. Explicit ``agent_name`` attribute (not in upstream today but cheap to
         honour if a fork adds it).
      2. First non-empty line of ``system_prompt`` (AcmeAssist's prod prompt
         starts with "You are AcmeAssist..." — that line is the natural label).
      3. ``None`` — the sinks fall back to an anonymous hash.
    """
    if options is None:
        return None
    name = getattr(options, "agent_name", None)
    if isinstance(name, str) and name:
        return name
    sp = getattr(options, "system_prompt", None)
    if isinstance(sp, str) and sp:
        first = sp.strip().splitlines()[0].strip()
        if first:
            return first[:80]
    return None


def _derive_agent_description(options: Any) -> str | None:
    """Pick an agent description from options.

    Mirrors :func:`_derive_agent_name` — the description is purely opt-in.
    Callers set it on options via ``options.agent_description = "..."`` and
    the proxy threads it through to the telemetry pipeline. Unlike the
    name, there's no implicit fallback to system_prompt: a description is
    a human label, not something we want to synthesize from the prompt.
    """
    if options is None:
        return None
    desc = getattr(options, "agent_description", None)
    if isinstance(desc, str) and desc:
        return desc
    return None
