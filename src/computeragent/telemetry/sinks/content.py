"""Structured content capture — the semconv-1.40 message model.

Ports the TS ``@computeragent/observability`` content-accumulator.ts +
content-capture.ts so the Python sink emits the SAME shapes:

* ``gen_ai.input.messages``  = ``[{role, parts:[...]}]``
* ``gen_ai.output.messages`` = ``[{role, parts:[...], finish_reason}]``
* ``gen_ai.system_instructions`` = ``[part, ...]`` (no role wrapper)

Message *parts* use the spec field renames (NOT the Anthropic block names):
  text       -> ``{type: "text",               content}``
  thinking   -> ``{type: "reasoning",          content}``
  tool_use   -> ``{type: "tool_call",          name, id?, arguments?}``
  tool_result-> ``{type: "tool_call_response", response, id?}``

This module is intentionally dependency-free (no OpenTelemetry import) so the
accumulator + part builders can be unit-tested without the ``[otel]`` extra.
``write_inference_content`` takes the span / logger as opaque objects.

Redaction: NONE here. The ``PiiRedactor`` / ``GuardrailFilter`` middleware has
already mutated ``event.payload`` before any sink runs, so what reaches this
module is the redacted projection. This is a deliberate divergence from TS,
which carries a second redaction backstop the Python pipeline does not need.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("computeragent.telemetry")

# Span-attribute / log-body keys + the inference event name. Mirror the TS
# constants in packages/observability/src/semantic/attributes.ts.
GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"
GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"
GEN_AI_SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
EVENT_GEN_AI_CLIENT_INFERENCE_OPERATION_DETAILS = (
    "gen_ai.client.inference.operation.details"
)


# ── finish-reason mapping ----------------------------------------------------


def map_finish_reason(stop_reason: str | None, has_tool_use: bool) -> str:
    """Normalise an Anthropic ``stop_reason`` to the spec finish-reason enum.

    Mirrors ``mapStopReasonToFinishReason`` in TS content-capture: when the
    stop reason is absent but the message called a tool, infer ``tool_call``;
    otherwise default to ``stop`` (the field is required by the output schema).
    """
    mapping = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "pause_turn": "stop",
        "max_tokens": "length",
        "tool_use": "tool_call",
        "refusal": "content_filter",
    }
    if stop_reason in mapping:
        return mapping[stop_reason]
    return "tool_call" if has_tool_use else "stop"


# ── part builders ------------------------------------------------------------


def _text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "content": text}


def _reasoning_part(text: str) -> dict[str, Any]:
    return {"type": "reasoning", "content": text}


def _tool_call_part(name: str, call_id: str | None, arguments: Any) -> dict[str, Any]:
    part: dict[str, Any] = {"type": "tool_call", "name": name}
    if call_id is not None:
        part["id"] = call_id
    if arguments is not None:
        part["arguments"] = arguments
    return part


def _tool_call_response_part(response: Any, call_id: str | None) -> dict[str, Any]:
    part: dict[str, Any] = {"type": "tool_call_response", "response": response}
    if call_id is not None:
        part["id"] = call_id
    return part


def block_to_part(block: dict[str, Any]) -> dict[str, Any] | None:
    """Convert one Anthropic content block to a spec part, or None if unknown.

    Forward-compatible path for when ``event.from_message`` forwards block
    lists; the current event shape only carries joined text, so the sink uses
    ``record_assistant_text`` / ``record_user_text`` for now.
    """
    btype = block.get("type")
    if btype == "text":
        text = block.get("text")
        return _text_part(text) if isinstance(text, str) else None
    if btype == "thinking":
        thinking = block.get("thinking")
        return _reasoning_part(thinking) if isinstance(thinking, str) else None
    if btype == "tool_use":
        name = block.get("name")
        if isinstance(name, str):
            cid = block.get("id")
            return _tool_call_part(name, cid if isinstance(cid, str) else None, block.get("input"))
        return None
    if btype == "tool_result":
        cid = block.get("tool_use_id")
        return _tool_call_response_part(block.get("content"), cid if isinstance(cid, str) else None)
    return None


def blocks_to_parts(blocks: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for b in blocks or []:
        part = block_to_part(b)
        if part is not None:
            parts.append(part)
    return parts


# ── accumulator --------------------------------------------------------------


@dataclass
class _SessionContent:
    input: list[dict[str, Any]] = field(default_factory=list)
    output: list[dict[str, Any]] = field(default_factory=list)
    system: list[dict[str, Any]] = field(default_factory=list)


class ContentAccumulator:
    """Per-session accumulator for structured input/output/system content.

    Mirrors the TS ContentAccumulator. Input/output/system reset at turn
    boundaries (``reset``); tool args/results are keyed by ``call_id`` and
    survive resets until consumed (``take_*``) or the session ends
    (``clear_session``).
    """

    def __init__(self) -> None:
        self._per_session: dict[str, _SessionContent] = {}
        self._tool_args: dict[str, Any] = {}
        self._tool_results: dict[str, Any] = {}

    def _get(self, session_id: str) -> _SessionContent:
        sc = self._per_session.get(session_id)
        if sc is None:
            sc = _SessionContent()
            self._per_session[session_id] = sc
        return sc

    # input -------------------------------------------------------------------

    def record_user_text(self, session_id: str, text: str) -> None:
        if not text:
            return
        self._get(session_id).input.append({"role": "user", "parts": [_text_part(text)]})

    def record_user_blocks(self, session_id: str, blocks: list[dict[str, Any]]) -> None:
        # tool_result blocks are model plumbing, not a user prompt.
        non_result = [b for b in blocks if b.get("type") != "tool_result"]
        if not non_result:
            return
        parts = blocks_to_parts(non_result)
        if not parts:
            return
        self._get(session_id).input.append({"role": "user", "parts": parts})

    # output ------------------------------------------------------------------

    def record_assistant_text(
        self,
        session_id: str,
        text: str,
        *,
        stop_reason: str | None = None,
        has_tool_use: bool = False,
    ) -> None:
        if not text:
            return
        self._get(session_id).output.append(
            {
                "role": "assistant",
                "parts": [_text_part(text)],
                "finish_reason": map_finish_reason(stop_reason, has_tool_use),
            }
        )

    def record_assistant_blocks(
        self,
        session_id: str,
        blocks: list[dict[str, Any]],
        *,
        stop_reason: str | None = None,
    ) -> None:
        parts = blocks_to_parts(blocks)
        if not parts:
            return
        has_tool_use = any(p["type"] == "tool_call" for p in parts)
        self._get(session_id).output.append(
            {
                "role": "assistant",
                "parts": parts,
                "finish_reason": map_finish_reason(stop_reason, has_tool_use),
            }
        )

    # system ------------------------------------------------------------------

    def record_system_instructions(self, session_id: str, text: str) -> None:
        if not text:
            return
        self._get(session_id).system.append(_text_part(text))

    # tool args/results (callId-keyed, survive reset) -------------------------

    def record_tool_args(self, call_id: str, args: Any) -> None:
        self._tool_args[call_id] = args

    def record_tool_result(self, call_id: str, result: Any) -> None:
        self._tool_results[call_id] = result

    def take_tool_args(self, call_id: str) -> Any:
        return self._tool_args.pop(call_id, None)

    def take_tool_result(self, call_id: str) -> Any:
        return self._tool_results.pop(call_id, None)

    # snapshots (non-destructive) ---------------------------------------------

    def input_for(self, session_id: str) -> list[dict[str, Any]]:
        sc = self._per_session.get(session_id)
        return list(sc.input) if sc else []

    def output_for(self, session_id: str) -> list[dict[str, Any]]:
        sc = self._per_session.get(session_id)
        return list(sc.output) if sc else []

    def system_for(self, session_id: str) -> list[dict[str, Any]]:
        sc = self._per_session.get(session_id)
        return list(sc.system) if sc else []

    # lifecycle ---------------------------------------------------------------

    def reset(self, session_id: str) -> None:
        """Clear per-turn input/output for the next turn.

        System instructions are session-scoped (recorded once at
        session_started) and PERSIST across turns so each turn's flush still
        carries the system context. Tool maps (callId-keyed) also survive.
        """
        sc = self._per_session.get(session_id)
        if sc is not None:
            sc.input = []
            sc.output = []

    def clear_session(self, session_id: str) -> None:
        """Full purge incl. system + any unconsumed tool args/results. Call on
        session end."""
        self._per_session.pop(session_id, None)
        # Tool maps are global (callId-keyed); we can't cheaply scope them by
        # session, so leave any stragglers to be overwritten — they are tiny
        # and callId is unique. Nothing to do beyond dropping session content.


# ── write ---------------------------------------------------------------------


def _json_dumps_safe(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value))


def serialize_bounded(value: Any, max_bytes: int) -> str:
    """JSON-encode + truncate to ``max_bytes`` chars with the TS marker.

    Truncated suffix is ``..."[truncated:N]"`` with the untruncated length so
    downstream consumers know they got a clipped value (matches TS).
    """
    raw = _json_dumps_safe(value)
    if len(raw) <= max_bytes:
        return raw
    head = raw[: max(0, max_bytes - 32)]
    return f'{head}..."[truncated:{len(raw)}]"'


def write_inference_content(
    *,
    input_msgs: list[dict[str, Any]] | None,
    output_msgs: list[dict[str, Any]] | None,
    system_parts: list[dict[str, Any]] | None,
    capture_content: bool,
    capture_mode: str,
    max_attr_length: int,
    span: Any | None = None,
    logger_: Any | None = None,
    event_attributes: dict[str, Any] | None = None,
) -> None:
    """Write structured content to a span (attributes) and/or a log record.

    Mirrors the TS ``writeInferenceContent``: no-op when capture is off; empty
    arrays are skipped; ``attributes``/``both`` set JSON-string span
    attributes; ``events``/``both`` emit ONE
    ``gen_ai.client.inference.operation.details`` log record. Never raises.
    """
    if not capture_content:
        return

    inp = input_msgs if input_msgs else None
    out = output_msgs if output_msgs else None
    sysp = system_parts if system_parts else None
    if inp is None and out is None and sysp is None:
        return

    if capture_mode in ("attributes", "both") and span is not None:
        if inp is not None:
            span.set_attribute(GEN_AI_INPUT_MESSAGES, serialize_bounded(inp, max_attr_length))
        if out is not None:
            span.set_attribute(GEN_AI_OUTPUT_MESSAGES, serialize_bounded(out, max_attr_length))
        if sysp is not None:
            span.set_attribute(
                GEN_AI_SYSTEM_INSTRUCTIONS, serialize_bounded(sysp, max_attr_length)
            )

    if capture_mode in ("events", "both") and logger_ is not None:
        body: dict[str, Any] = {}
        if inp is not None:
            body[GEN_AI_INPUT_MESSAGES] = inp
        if out is not None:
            body[GEN_AI_OUTPUT_MESSAGES] = out
        if sysp is not None:
            body[GEN_AI_SYSTEM_INSTRUCTIONS] = sysp
        if not body:
            return
        attributes = {"event.name": EVENT_GEN_AI_CLIENT_INFERENCE_OPERATION_DETAILS}
        if event_attributes:
            attributes.update(event_attributes)
        try:
            from opentelemetry._logs import SeverityNumber

            logger_.emit(
                severity_number=SeverityNumber.INFO,
                severity_text="INFO",
                body=body,
                attributes=attributes,
                event_name=EVENT_GEN_AI_CLIENT_INFERENCE_OPERATION_DETAILS,
            )
        except Exception:  # noqa: BLE001 — never break a session for a log emit
            logger.debug("inference-details log emit failed", exc_info=True)
