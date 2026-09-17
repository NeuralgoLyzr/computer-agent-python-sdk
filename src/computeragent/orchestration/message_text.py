"""Pull text out of an opaque engine message payload, defensively.

Handles the Claude Agent SDK shapes: a terminal ``ResultMessage`` and an
``AssistantMessage`` whose ``content`` holds ``TextBlock``s — as objects (what
the Python SDK yields) or as the dicts the TS wire form uses. Unknown shapes are
ignored.

Shared by the worker-result collector and the roster-text tap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


def _field(obj: Any, key: str) -> Any:
    """Read ``key`` off a dict OR an object.

    The Python SDK yields dataclass-ish message objects where the TS SDK has
    plain JSON, and a worker's payload can be either (a stub engine in a test,
    a real ``AssistantMessage`` in a run). Branching on ``isinstance(dict)``
    alone silently matched nothing for the object form.
    """
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _type_of(payload: Any) -> str | None:
    """The discriminator, for payloads that carry one.

    The Python SDK's message classes have no ``type`` attribute — they are
    distinguished by class — so this returns ``None`` for them and the caller
    falls through to the shape checks below.
    """
    t = _field(payload, "type")
    return t if isinstance(t, str) else None


def extract_message_text(
    payload: Any,
    on_text: Callable[[str], None],
    on_result: Callable[[str], None],
) -> None:
    if payload is None or isinstance(payload, (str, bytes, int, float, bool)):
        return

    kind = _type_of(payload)

    # A terminal result. The Python SDK's ResultMessage has no `type` but does
    # carry `result`, and only a result message does — so the class name check
    # is not needed to tell it apart.
    result = _field(payload, "result")
    if (kind == "result" or kind is None) and isinstance(result, str):
        on_result(result)
        return

    if kind == "text":
        text = _field(payload, "text")
        if isinstance(text, str):
            on_text(text)
        return

    message = _field(payload, "message")
    content = _field(message, "content") if message is not None else None
    if content is None:
        content = _field(payload, "content")
    if isinstance(content, list):
        for block in content:
            btype = _type_of(block)
            btext = _field(block, "text")
            # `TextBlock` has `.text` and no `.type`; the dict form has both.
            if isinstance(btext, str) and btype in (None, "text"):
                on_text(btext)


__all__ = ["extract_message_text"]
