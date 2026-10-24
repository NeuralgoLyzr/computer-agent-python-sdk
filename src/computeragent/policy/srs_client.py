"""Lyzr SRS v1 calls — the input check and the two tool-call checks.

A port of the requests CAS's harness makes (harness-server
``srs-policy-decider.ts`` and ``policy-tool-name.ts``):

* :func:`check_input` — ``POST /v1/rai/inference`` stage ``llm_input`` on a
  user message: block, mask, or pass.
* :func:`check_tool_content` — ``POST /v1/rai/inference`` stage ``llm_output``
  on a tool call's string arguments: SRS's content checks on what the tool is
  about to be handed.
* :func:`check_tool_call` — ``POST /v1/guardrails/evaluate-tool-call``: each
  policy's Cedar then OPA over the structured call, in order, first deny wins.

Every call takes the whole policy list and sends the caller's key on
``x-api-key``. A call that reaches no verdict (transport failure, non-2xx)
raises :class:`SrsUnavailable`; deciding what that means is the caller's.
Nothing here logs text, tool arguments, the key, or an SRS response body: an
error body can echo the request, and the request is the prompt.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("computeragent.policy.srs")

#: Namespace marker Anthropic-shaped engines use for MCP tools.
MCP_PREFIX = "mcp__"


class SrsUnavailable(RuntimeError):
    """An SRS call reached no verdict.

    ``status`` is the HTTP status, or ``None`` when no response arrived.
    ``permanent`` marks a status that answers the same for every later call
    (403 no RAI permission, 404 a policy gone, 422 a body SRS rejects). The
    message is egress-safe: the status and SRS's machine codes, never the body.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.permanent = status in (403, 404, 422)


@dataclass(frozen=True)
class InputVerdict:
    """SRS's verdict on one user message."""

    text: str
    blocked: bool
    outcome: str  # "blocked" | "redacted" | "clean"
    reason: str | None = None
    blocked_by_policy_id: str | None = None
    #: Masked entity type → count, e.g. ``{"EMAIL_ADDRESS": 1}``. Never values.
    entity_counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Violation:
    """A tool-call check that denied."""

    denied_by: str  # "rai" (content check), "cedar" / "opa" / "srs" (tool-call check)
    reason: str
    policy_id: str | None = None


# ── tool naming (policy-tool-name.ts) ────────────────────────────────────────


def policy_tool_name(tool_name: str, *, engine_name: str, mcp_server_names: Sequence[str]) -> str:
    """The name a tool call is presented to Cedar under.

    Built-ins belong to the engine, MCP tools to the server::

        claude-agent-sdk  Bash                       ->  claude-agent-sdk__Bash
        gitagent          write                      ->  gitagent__write
        claude-agent-sdk  mcp__probe__get_build_info ->  (unchanged)
        gitagent          probe__get_build_info      ->  mcp__probe__get_build_info

    The engines' vocabularies overlap with different argument shapes, and
    Cedar is case-sensitive and default-deny, so a bare name would make a rule
    land on the wrong engine's tool or deny the other's outright.
    """
    if tool_name.startswith(MCP_PREFIX):
        return tool_name
    sep = tool_name.find("__")
    if sep > 0 and tool_name[:sep] in mcp_server_names:
        return f"{MCP_PREFIX}{tool_name}"
    return f"{engine_name}__{tool_name}"


def mcp_server_names_of(options: Any) -> list[str]:
    """The MCP server names on an engine options mapping (``mcp_servers``)."""
    if not isinstance(options, Mapping):
        return []
    servers = options.get("mcp_servers")
    return [str(name) for name in servers] if isinstance(servers, Mapping) else []


def tool_args_text_blob(args: Mapping[str, Any] | None) -> str:
    """Every top-level non-empty string argument, newline-joined in order.

    The same flattening Lyzr's agent runtime applies before the content check,
    so one policy scans a tool call identically wherever it ran. Names,
    numbers, booleans and nested values are dropped (the tool-call check sees
    those). Empty means there is nothing to scan and no request is made.
    """
    if not args:
        return ""
    return "\n".join(v for v in args.values() if isinstance(v, str) and v)


# ── transport ────────────────────────────────────────────────────────────────


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or ''}{f':{parts.port}' if parts.port else ''}"


def _status_error(resp: Any) -> SrsUnavailable:
    """An egress-safe error for a non-2xx: the status plus SRS's machine codes
    (``detail[].type`` on a 422), never ``detail`` itself."""
    codes: list[str] = []
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict) and isinstance(body.get("detail"), list):
        for item in body["detail"]:
            kind = item.get("type") if isinstance(item, dict) else None
            if isinstance(kind, str) and kind:
                codes.append(kind)
    suffix = f" ({', '.join(codes[:5])})" if codes else ""
    return SrsUnavailable(f"SRS {resp.status_code}{suffix}", status=resp.status_code)


async def _post(
    srs_url: str,
    path: str,
    body: dict[str, Any],
    *,
    api_key: str,
    timeout_s: float,
    client: Any | None,
) -> dict[str, Any]:
    import httpx

    url = f"{srs_url.rstrip('/')}{path}"
    headers = {"content-type": "application/json", "x-api-key": api_key}
    try:
        if client is not None:
            resp = await client.post(url, headers=headers, json=body, timeout=timeout_s)
        else:
            async with httpx.AsyncClient(timeout=timeout_s) as owned:
                resp = await owned.post(url, headers=headers, json=body)
    except Exception as exc:  # network / DNS / TLS / timeout: no body to echo
        logger.warning("SRS %s unreachable at %s (%s)", path, _origin(url), type(exc).__name__)
        raise SrsUnavailable(f"SRS unreachable: {type(exc).__name__}") from None
    if resp.status_code >= 300:
        error = _status_error(resp)
        logger.warning(
            "SRS %s returned %s (%s bytes)", path, resp.status_code, len(resp.content or b"")
        )
        raise error
    try:
        out = resp.json()
    except Exception:
        raise SrsUnavailable("SRS returned malformed JSON", status=resp.status_code) from None
    return out if isinstance(out, dict) else {}


def _warnings(out: Mapping[str, Any]) -> list[str]:
    value = out.get("warnings")
    if isinstance(value, str):
        return [value]
    return [w for w in value if isinstance(w, str)] if isinstance(value, list) else []


_TOKEN = re.compile(r"^<?(.+?)_\d+>?$")


def _entity_counts(out: Mapping[str, Any]) -> dict[str, int]:
    """Entity type → count from the mask tokens (``redaction_map`` VALUES, e.g.
    ``<EMAIL_ADDRESS_1>``). The map's KEYS are the raw values and are never
    read."""
    counts: dict[str, int] = {}
    redaction_map = out.get("redaction_map")
    if isinstance(redaction_map, Mapping):
        for token in redaction_map.values():
            if isinstance(token, str):
                match = _TOKEN.match(token.strip())
                kind = (match.group(1) if match else "PII").upper()
                counts[kind] = counts.get(kind, 0) + 1
    return counts


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


# ── the three calls ──────────────────────────────────────────────────────────


async def check_input(
    srs_url: str,
    *,
    api_key: str,
    policy_ids: Sequence[str],
    text: str,
    principal_id: str,
    timeout_s: float,
    client: Any | None = None,
) -> InputVerdict:
    """``llm_input`` on one piece of user text. Raises :class:`SrsUnavailable`
    on no verdict."""
    out = await _post(
        srs_url,
        "/v1/rai/inference",
        {
            "policy_ids": list(policy_ids),
            "input_text": text,
            "stage": "llm_input",
            "principal_id": principal_id,
        },
        api_key=api_key,
        timeout_s=timeout_s,
        client=client,
    )
    processed = out.get("processed_text")
    masked = processed if isinstance(processed, str) else text
    blocked = bool(out.get("blocked"))
    warnings = _warnings(out)
    if warnings and not blocked:
        # A check that threw is isolated into a warning and reported clean.
        logger.warning("SRS input check finished with %d warning(s)", len(warnings))
    return InputVerdict(
        text=masked,
        blocked=blocked,
        outcome="blocked" if blocked else ("redacted" if masked != text else "clean"),
        reason=_optional_str(out.get("block_reason")),
        blocked_by_policy_id=_optional_str(out.get("blocked_by_policy_id")) if blocked else None,
        entity_counts=_entity_counts(out),
    )


async def check_tool_content(
    srs_url: str,
    *,
    api_key: str,
    policy_ids: Sequence[str],
    tool_name: str,
    tool_args: Mapping[str, Any] | None,
    call_id: str | None,
    agent_name: str,
    session_id: str,
    principal_id: str,
    timeout_s: float,
    client: Any | None = None,
) -> Violation | None:
    """``llm_output`` on a tool call's string arguments. ``None`` when it
    passed or there was nothing to scan (no request is made then).

    ``processed_text`` is ignored on purpose: a policy can veto a tool call,
    it cannot rewrite the arguments the model asked for.
    """
    blob = tool_args_text_blob(tool_args)
    if not blob:
        logger.info("SRS content check skipped for %s: no string-valued arguments", tool_name)
        return None
    try:
        arguments = json.dumps(dict(tool_args or {}))
    except (TypeError, ValueError):
        arguments = "{}"
    call: dict[str, Any] = {
        "type": "function",
        "function": {"name": tool_name, "arguments": arguments},
    }
    if call_id:
        call = {"id": call_id, **call}
    out = await _post(
        srs_url,
        "/v1/rai/inference",
        {
            "policy_ids": list(policy_ids),
            "input_text": blob,
            "messages": [{"role": "assistant", "content": "", "tool_calls": [call]}],
            "agent_id": agent_name,
            "session_id": session_id,
            "stage": "llm_output",
            # Never served from cache: the same arguments in another session
            # are a different decision.
            "use_cache": False,
            "principal_id": principal_id,
        },
        api_key=api_key,
        timeout_s=timeout_s,
        client=client,
    )
    blocked = bool(out.get("blocked"))
    warnings = _warnings(out)
    if warnings and not blocked:
        logger.warning(
            "SRS content check for %s finished with %d warning(s)", tool_name, len(warnings)
        )
    if not blocked:
        return None
    return Violation(
        denied_by="rai",
        reason=_optional_str(out.get("block_reason"))
        or (warnings[0] if warnings else "tool arguments blocked by content policy"),
        policy_id=_optional_str(out.get("blocked_by_policy_id")),
    )


async def check_tool_call(
    srs_url: str,
    *,
    api_key: str,
    policy_ids: Sequence[str],
    tool_name: str,
    raw_tool_name: str,
    tool_args: Mapping[str, Any] | None,
    agent_name: str,
    principal_id: str,
    timeout_s: float,
    client: Any | None = None,
) -> Violation | None:
    """Cedar then OPA over the structured call. ``None`` only on an explicit
    ``allowed: true``: a missing or non-boolean ``allowed`` is a deny, so a
    renamed field never reads as permission."""
    out = await _post(
        srs_url,
        "/v1/guardrails/evaluate-tool-call",
        {
            "policy_ids": list(policy_ids),
            "tool_name": tool_name,
            "raw_tool_name": raw_tool_name,
            "tool_args": dict(tool_args or {}),
            "principal_id": principal_id,
            "bundle_id": agent_name,
            "bundle_name": agent_name,
        },
        api_key=api_key,
        timeout_s=timeout_s,
        client=client,
    )
    allowed = out.get("allowed")
    if allowed is True:
        return None
    reason = _optional_str(out.get("reason")) or (
        "denied by RAI policy" if isinstance(allowed, bool) else "SRS returned no `allowed` field"
    )
    return Violation(
        denied_by=_optional_str(out.get("denied_by")) or "srs",
        reason=reason,
        policy_id=_optional_str(out.get("denied_by_policy_id")),
    )


__all__ = [
    "InputVerdict",
    "SrsUnavailable",
    "Violation",
    "check_input",
    "check_tool_call",
    "check_tool_content",
    "mcp_server_names_of",
    "policy_tool_name",
    "tool_args_text_blob",
]
