"""Guardrail checks through the egress proxy — ``POST {guardrails_url}/v1/check``.

The egress proxy forwards the call to the agent gateway, which applies the
org's RAI policies and then the agent's own (contract ``agent-gateway-v1``
§7.3); the SDK sends no policy ids and decides no fail-open of its own.

* :func:`check_text` — op ``input`` on one piece of user text: allow, rewrite
  (the gateway's masked text replaces it) or block.
* :func:`check_tool_call` — op ``tool-input`` on one tool call, identified by
  the engine's own tool-use id: allow or deny. Arguments are never rewritten.

A call that reaches no verdict (transport failure, timeout, non-2xx,
malformed answer) raises :class:`GuardrailsUnavailable`; the caller blocks.
Nothing here logs text, tool arguments, the key or a response body.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("computeragent.policy.guardrails")

#: The check route, relative to ``guardrails_url``.
CHECK_PATH = "/v1/check"

#: Per-call timeout for a guardrails check.
GUARDRAILS_TIMEOUT_S = 5.0

#: Namespace marker Anthropic-shaped engines use for MCP tools.
MCP_PREFIX = "mcp__"


class GuardrailsUnavailable(RuntimeError):
    """A guardrails check reached no verdict.

    ``status`` is the HTTP status, or ``None`` when no response arrived. The
    message is egress-safe: the status and the error code, never the body.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class TextVerdict:
    """The gateway's verdict on one piece of user text."""

    text: str
    blocked: bool
    outcome: str  # "blocked" | "redacted" | "clean"
    reason: str | None = None
    blocked_by_policy_id: str | None = None
    #: The gateway applied its fail-open and did not check the text.
    skipped: bool = False


@dataclass(frozen=True)
class Violation:
    """A tool call the gateway denied."""

    denied_by: str
    reason: str
    policy_id: str | None = None


# ── tool naming ──────────────────────────────────────────────────────────────


def policy_tool_name(tool_name: str, *, engine_name: str, mcp_server_names: Sequence[str]) -> str:
    """The name a tool call is presented to the policies under.

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


# ── transport ────────────────────────────────────────────────────────────────


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or ''}{f':{parts.port}' if parts.port else ''}"


def _status_error(resp: Any) -> GuardrailsUnavailable:
    """An egress-safe error for a non-2xx: the status and the error code
    (``{"error": {"code"}}``), never the message or the body."""
    code = None
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        value = body["error"].get("code")
        code = value if isinstance(value, str) and value else None
    suffix = f" ({code})" if code else ""
    return GuardrailsUnavailable(f"guardrails {resp.status_code}{suffix}", status=resp.status_code)


async def _check(
    guardrails_url: str,
    body: dict[str, Any],
    *,
    api_key: str,
    timeout_s: float,
    client: Any | None,
) -> dict[str, Any]:
    import httpx

    url = f"{guardrails_url.rstrip('/')}{CHECK_PATH}"
    headers = {"content-type": "application/json", "x-api-key": api_key}
    try:
        if client is not None:
            resp = await client.post(url, headers=headers, json=body, timeout=timeout_s)
        else:
            async with httpx.AsyncClient(timeout=timeout_s) as owned:
                resp = await owned.post(url, headers=headers, json=body)
    except Exception as exc:  # network / DNS / TLS / timeout: no body to echo
        logger.warning("guardrails check unreachable at %s (%s)", _origin(url), type(exc).__name__)
        raise GuardrailsUnavailable(f"guardrails unreachable: {type(exc).__name__}") from None
    if resp.status_code >= 300:
        error = _status_error(resp)
        log = logger.error if resp.status_code == 400 else logger.warning
        log("guardrails check returned %s (%s bytes)", resp.status_code, len(resp.content or b""))
        raise error
    try:
        out = resp.json()
    except Exception:
        out = None
    data = out.get("data") if isinstance(out, dict) else None
    if not isinstance(data, dict):
        raise GuardrailsUnavailable(
            "guardrails returned a malformed answer", status=resp.status_code
        )
    warnings = data.get("warnings")
    if isinstance(warnings, int) and warnings > 0:
        logger.warning("guardrails check finished with %d warning(s)", warnings)
    if data.get("skipped") is True:
        logger.info("guardrails skipped (fail-open by the gateway)")
    return data


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


# ── the two checks ───────────────────────────────────────────────────────────


async def check_text(
    guardrails_url: str,
    *,
    api_key: str,
    text: str,
    timeout_s: float = GUARDRAILS_TIMEOUT_S,
    client: Any | None = None,
) -> TextVerdict:
    """Op ``input`` on one piece of user text. Raises
    :class:`GuardrailsUnavailable` on no verdict."""
    data = await _check(
        guardrails_url,
        {"op": "input", "subject": {"text": text}},
        api_key=api_key,
        timeout_s=timeout_s,
        client=client,
    )
    decision = data.get("decision")
    skipped = data.get("skipped") is True
    if decision == "allow":
        return TextVerdict(text=text, blocked=False, outcome="clean", skipped=skipped)
    if decision == "rewrite":
        rewritten = data.get("text")
        masked = rewritten if isinstance(rewritten, str) else text
        return TextVerdict(
            text=masked,
            blocked=False,
            outcome="redacted" if masked != text else "clean",
            skipped=skipped,
        )
    if decision != "block":
        logger.warning("guardrails returned an unknown decision; blocking the message")
    return TextVerdict(
        text=text,
        blocked=True,
        outcome="blocked",
        reason=_optional_str(data.get("blockReason")),
        blocked_by_policy_id=_optional_str(data.get("blockedByPolicyId")),
    )


async def check_tool_call(
    guardrails_url: str,
    *,
    api_key: str,
    call_id: str,
    tool_name: str,
    tool_args: Mapping[str, Any] | None,
    timeout_s: float = GUARDRAILS_TIMEOUT_S,
    client: Any | None = None,
) -> Violation | None:
    """Op ``tool-input`` on one tool call. ``None`` when allowed; a decision
    outside allow/rewrite/block denies."""
    if not call_id:
        raise ValueError("a tool call checked by the guardrails needs the engine's tool-use id")
    data = await _check(
        guardrails_url,
        {
            "op": "tool-input",
            "subject": {
                "toolCalls": [
                    {"id": call_id, "name": tool_name, "arguments": dict(tool_args or {})}
                ]
            },
        },
        api_key=api_key,
        timeout_s=timeout_s,
        client=client,
    )
    results = data.get("results")
    first = (
        results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else {}
    )
    decision = data.get("decision")
    if decision == "block" or first.get("decision") == "block":
        return Violation(
            denied_by=_optional_str(first.get("deniedBy")) or "guardrails",
            reason=_optional_str(first.get("reason"))
            or _optional_str(data.get("blockReason"))
            or "denied by RAI policy",
            policy_id=_optional_str(first.get("policyId"))
            or _optional_str(data.get("blockedByPolicyId")),
        )
    if decision in ("allow", "rewrite"):
        return None
    logger.warning("guardrails returned an unknown decision for %s; denying", tool_name)
    return Violation(denied_by="guardrails", reason="guardrails returned an unknown decision")


__all__ = [
    "CHECK_PATH",
    "GUARDRAILS_TIMEOUT_S",
    "GuardrailsUnavailable",
    "TextVerdict",
    "Violation",
    "check_text",
    "check_tool_call",
    "mcp_server_names_of",
    "policy_tool_name",
]
