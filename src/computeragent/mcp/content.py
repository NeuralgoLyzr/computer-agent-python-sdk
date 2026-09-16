"""Coerce a Lyzr facade tool result into content the in-process MCP path keeps.

Same *intent* as ``normalizeContent`` (``llm-proxy-openai/src/mcp.ts:362-428``) —
never drop what the tool returned — but deliberately **not** a transcription of
it, because the sink is different and narrower.

The TS bridge emits MCP over the wire, where the full content union and
``structuredContent`` both exist. Here the consumer is ``create_sdk_mcp_server``
in this process, and it discards two things the wire would have carried:

===============================================  ====================================
upstream (``claude_agent_sdk`` 0.2.153)          consequence
===============================================  ====================================
``run_tool`` builds ``CallToolResult`` from       ``structuredContent`` has no path to
only ``content`` + ``isError``                    the CLI, so hoisting a ``structured``
                                                  block out of ``content`` — what
                                                  ``mcp.ts:413`` does — would DELETE
                                                  the tool's payload
``_convert_tool_content`` accepts only            ``audio`` is in MCP's union and in
``text`` / ``image`` / ``resource_link`` /        ``mcp.ts``'s pass-through set, but
``resource``; everything else is dropped          passing it on gets it dropped
                                                  with a warning
===============================================  ====================================

So the rule here is one line: **exactly the four types upstream can convert
survive as blocks; everything else becomes a text block.** A text block always
validates and always reaches the model, so nothing is lost — which is the
property the TS function exists to guarantee, expressed against a different sink.

Why this matters concretely: the TS comment records that passing non-union
blocks through made the Claude SDK reject the *whole* result with
``invalid_union``, so a call that had actually SUCCEEDED surfaced to the model as
``isError: true``. Every tool call through the facade hit it.

If upstream ever widens what it converts, ``test_mcp_content.py`` has a guard
that fails — so this file gets revisited deliberately rather than drifting.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger("computeragent.mcp.content")

#: Block types ``claude_agent_sdk._convert_tool_content`` can actually convert.
#: Narrower than MCP's own union (which also has ``audio``) — see the module
#: docstring. Anything outside this set is stringified rather than passed on.
CONVERTIBLE = frozenset({"text", "image", "resource_link", "resource"})


def normalize_content(result: Any) -> list[dict[str, Any]]:
    """Facade ``result`` → a list of content blocks that survive the round trip.

    Never raises: a tool result is data from a remote service, and a coercion
    failure must not become the turn's failure.
    """
    if result is None:
        return []
    if isinstance(result, str):
        return [_text(result)]

    blocks = result if isinstance(result, list) else [result]
    content: list[dict[str, Any]] = []
    # Logged once per call rather than once per block: a facade that starts
    # emitting a NEW unconvertible type is exactly how this broke the first
    # time, and it was invisible until a model reported a failure.
    coerced: list[str] = []

    for block in blocks:
        if not isinstance(block, Mapping):
            content.append(_text(str(block)))
            coerced.append(type(block).__name__)
            continue
        kind = block.get("type")
        if isinstance(kind, str) and kind in CONVERTIBLE:
            content.append(dict(block))
        else:
            # Includes `structured` and `audio`. Both would be silently lost if
            # handled the way the TS handles them; as text they reach the model.
            content.append(_text(_dumps(block)))
            coerced.append(str(kind))

    if coerced:
        logger.warning(
            "coerced %d non-convertible content block(s) to text: %s — "
            "claude_agent_sdk would have dropped these outright",
            len(coerced),
            ",".join(sorted(set(coerced))),
        )
    return content


def _text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def _dumps(value: Any) -> str:
    """JSON if it will serialise, ``repr`` if it won't. Dropping it is not an
    option — it is whatever the tool actually returned."""
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):  # pragma: no cover - default=str covers nearly all
        return repr(value)


__all__ = ["CONVERTIBLE", "normalize_content"]
