"""``GitAgentEngine`` — Python port of the TypeScript ``engine-gitagent``.

The TS engine wraps ``import { query } from "gitclaw"``. There is no
Python ``gitclaw`` package — this module re-implements the same loop
directly against the appropriate provider SDK based on the requested
model.

Provider dispatch (from ``manifest.options["model"]``):

* ``anthropic:claude-...`` / bare ``claude-...`` → native Anthropic
  Messages API via the ``anthropic`` SDK with ``ANTHROPIC_API_KEY``.
* ``openai:gpt-...`` / bare ``gpt-...`` / unknown → OpenAI-compatible
  Chat Completions API via the ``openai`` SDK with
  ``GITCLAW_MODEL_BASE_URL`` + ``OPENAI_API_KEY``.

Env contract mirrors the TS engine when targeting OpenAI-compatible
gateways and matches ``claude-agent-sdk`` for the Anthropic path so the
same ``.env`` serves both modes.

Tool execution is **MCP-only** in 0.2.0 — both backends emit
``permission_request`` events and route the result through
``ctx.on_permission_request``, but the engine itself does not run
filesystem tools. Wire an MCP server through ``manifest.options`` if
you need real tool dispatch. Built-in tool runners land in 0.3.0.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any

from ..protocol_types import (
    EngineCapabilities,
    EngineContext,
    EngineEvent,
    PermissionRequest,
)

logger = logging.getLogger("computeragent.engines.git_agent")

_CAPABILITIES = EngineCapabilities(
    streaming_input=True,
    partial_messages=False,
    permission_callback=True,
    sessions=True,
    budget=False,
)

_MISSING_OPENAI_HINT = (
    "GitAgentEngine (OpenAI backend) requires the optional ``openai`` "
    "package. Install with: pip install 'computer-agent-py[gitagent]'"
)
_MISSING_ANTHROPIC_HINT = (
    "GitAgentEngine (Anthropic backend) requires the optional ``anthropic`` "
    "package. Install with: pip install 'computer-agent-py[gitagent]'"
)
_DEFAULT_MAX_TURNS = 10
_DEFAULT_MAX_TOKENS = 4096


class GitAgentEngine:
    """Engine driver that re-implements the gitclaw loop in Python with
    pluggable provider backends."""

    name = "gitagent"
    capabilities = _CAPABILITIES

    async def start_session(self, ctx: EngineContext) -> AsyncIterator[EngineEvent]:
        manifest_options = dict(ctx.manifest.options or {})
        raw_model: str | None = manifest_options.get("model")
        if not raw_model:
            raise ValueError(
                "GitAgentEngine requires options['model'] (e.g. 'gpt-4o-mini' "
                "or 'anthropic:claude-sonnet-4-5-20250929')"
            )
        backend = _select_backend(raw_model)
        bare_model = _strip_provider_prefix(raw_model)
        logger.debug(
            "GitAgentEngine: dispatching to %s backend for model %r",
            backend,
            raw_model,
        )
        if backend == "anthropic":
            iterator = _run_anthropic_session(
                ctx, manifest_options=manifest_options, model=bare_model
            )
        else:
            iterator = _run_openai_session(ctx, manifest_options=manifest_options, model=bare_model)
        async for ev in iterator:
            yield ev


# ── backend selection ──────────────────────────────────────────────────


def _select_backend(model: str) -> str:
    """Pick a provider backend from the model name.

    Default is ``openai`` so the original gitclaw-shaped path is the
    fallback. Explicit ``anthropic:`` prefix or bare ``claude-`` model
    routes to the Anthropic backend.
    """
    lowered = model.lower()
    if lowered.startswith("anthropic:"):
        return "anthropic"
    if lowered.startswith("claude-") or lowered.startswith("claude_"):
        return "anthropic"
    if lowered.startswith("openai:"):
        return "openai"
    if lowered.startswith(("gpt-", "o1-", "o3-", "o4-")):
        return "openai"
    return "openai"


def _strip_provider_prefix(model: str) -> str:
    """``anthropic:claude-…`` → ``claude-…``."""
    if ":" not in model:
        return model
    provider, _, rest = model.partition(":")
    if provider.lower() in {"anthropic", "openai", "bedrock", "lyzr"}:
        return rest
    return model


# ── Anthropic backend ──────────────────────────────────────────────────


async def _run_anthropic_session(
    ctx: EngineContext,
    *,
    manifest_options: dict[str, Any],
    model: str,
) -> AsyncIterator[EngineEvent]:
    """Native Anthropic Messages API loop.

    Maps gitagent-shaped manifest options to ``messages.create`` kwargs:

    * ``system_prompt`` → ``system``
    * ``max_turns`` → loop bound (tool-use rounds)
    * ``constraints.temperature`` → ``temperature``
    * ``constraints.max_tokens`` → ``max_tokens``
    * ``tools_hint`` → not wired in 0.2.0 (MCP-only)
    """
    client = _build_anthropic_client(ctx.envs)

    system_prompt: str = manifest_options.get("system_prompt", "") or ""
    max_turns: int = int(manifest_options.get("max_turns", _DEFAULT_MAX_TURNS))
    constraints = manifest_options.get("constraints") or {}
    if not isinstance(constraints, dict):
        constraints = {}
    temperature = constraints.get("temperature")
    max_tokens = int(constraints.get("max_tokens", _DEFAULT_MAX_TOKENS))

    prior_turns: list[dict[str, Any]] = []
    async for user_msg in ctx.user_message_queue:
        if ctx.cancel_event.is_set():
            return

        # Anthropic wants ``system`` as a top-level arg, NOT in messages.
        # Restore prior turns into the system suffix so the agent has
        # context across .chat() calls — parity with the OpenAI path.
        system_suffix = _serialize_prior_turns(prior_turns)
        full_system = system_prompt
        if system_suffix:
            full_system = (
                f"{system_prompt}\n\n{system_suffix}".strip() if system_prompt else system_suffix
            )

        # Build the messages list — just the current turn (prior turns
        # are encoded into the system).
        user_content = user_msg.get("content")
        if not isinstance(user_content, (str, list)):
            user_content = str(user_content)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
        final_assistant_text = ""

        for _turn_idx in range(max_turns):
            if ctx.cancel_event.is_set():
                return

            kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if full_system:
                kwargs["system"] = full_system
            if temperature is not None:
                kwargs["temperature"] = temperature

            resp = await client.messages.create(**kwargs)

            usage = getattr(resp, "usage", None)
            yield EngineEvent(
                kind="usage_snapshot",
                cost_semantic="delta",
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
                cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", None),
                cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None),
            )
            yield EngineEvent(kind="sdk_message", payload=resp)

            # Collect text + tool_use blocks. Anthropic returns a list of
            # ``ContentBlock`` objects with ``.type`` discriminator.
            blocks = list(getattr(resp, "content", []) or [])
            text_parts: list[str] = []
            tool_blocks: list[Any] = []
            for block in blocks:
                btype = getattr(block, "type", None)
                if btype == "text":
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        text_parts.append(text)
                elif btype == "tool_use":
                    tool_blocks.append(block)
            final_assistant_text = "".join(text_parts)

            stop_reason = getattr(resp, "stop_reason", None)
            if stop_reason != "tool_use" and not tool_blocks:
                # Natural end-of-turn — no more tool calls.
                break

            # Persist the assistant turn so the next iteration can build
            # on it. Anthropic requires the prior assistant message to be
            # in ``messages`` before the tool_result user message.
            messages.append({"role": "assistant", "content": blocks})

            tool_results: list[dict[str, Any]] = []
            for tb in tool_blocks:
                permission_req = PermissionRequest(
                    call_id=getattr(tb, "id", ""),
                    tool_name=getattr(tb, "name", ""),
                    input=dict(getattr(tb, "input", {}) or {}),
                )
                decision = await ctx.on_permission_request(permission_req)
                if decision.get("behavior") == "deny":
                    output = f"denied by policy: {decision.get('message', 'no reason supplied')}"
                else:
                    output = (
                        "tool execution is not built into GitAgentEngine "
                        "in 0.2.0 — wire an MCP server for filesystem tools."
                    )
                tr_block = {
                    "type": "tool_result",
                    "tool_use_id": getattr(tb, "id", ""),
                    "content": output,
                }
                tool_results.append(tr_block)
                yield EngineEvent(
                    kind="sdk_message",
                    payload={
                        "role": "tool",
                        "tool_call_id": getattr(tb, "id", ""),
                        "content": output,
                    },
                )

            if not tool_results:
                # Defensive — stop_reason claimed tool_use but no blocks?
                break
            messages.append({"role": "user", "content": tool_results})

        prior_turns.append({"user": user_msg, "assistant": final_assistant_text})


def _env(envs: Mapping[str, str] | None, name: str) -> str | None:
    """The harness-supplied value first, the process environment second.

    The harness hands an engine its environment on ``EngineContext.envs`` and
    never exports it to ``os.environ`` — so reading only the process env made
    every gateway-routed value the coordinator computed invisible here, and a
    gateway-bound gitagent run failed claiming the variable was unset when the
    harness had in fact supplied it. The process env stays as the fallback for
    a standalone engine used outside the harness.
    """
    value = (envs or {}).get(name)
    return value if value else os.environ.get(name)


def _build_anthropic_client(envs: Mapping[str, str] | None = None) -> Any:
    try:
        import anthropic  # type: ignore
    except ImportError as exc:
        raise ImportError(_MISSING_ANTHROPIC_HINT) from exc
    api_key = _env(envs, "ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GitAgentEngine (Anthropic backend) requires ANTHROPIC_API_KEY — supply it via the "
            "agent's model gateway, ComputerAgent(envs=...), or the process environment."
        )
    base_url = _env(envs, "ANTHROPIC_BASE_URL")
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return anthropic.AsyncAnthropic(**kwargs)


# ── OpenAI backend (original gitclaw-shaped path) ──────────────────────


async def _run_openai_session(
    ctx: EngineContext,
    *,
    manifest_options: dict[str, Any],
    model: str,
) -> AsyncIterator[EngineEvent]:
    client = _build_openai_client(ctx.envs)

    system_prompt: str = manifest_options.get("system_prompt", "") or ""
    max_turns: int = int(manifest_options.get("max_turns", _DEFAULT_MAX_TURNS))
    tools_spec: list[dict[str, Any]] = list(manifest_options.get("tools") or [])
    constraints = manifest_options.get("constraints") or {}
    temperature = constraints.get("temperature") if isinstance(constraints, dict) else None

    prior_turns: list[dict[str, Any]] = []
    async for user_msg in ctx.user_message_queue:
        if ctx.cancel_event.is_set():
            return

        messages = _build_openai_messages(system_prompt, prior_turns, user_msg)
        final_assistant_text = ""

        for _turn_idx in range(max_turns):
            if ctx.cancel_event.is_set():
                return

            resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools_spec or None,
                temperature=temperature,
            )

            usage = getattr(resp, "usage", None)
            yield EngineEvent(
                kind="usage_snapshot",
                cost_semantic="delta",
                input_tokens=getattr(usage, "prompt_tokens", None),
                output_tokens=getattr(usage, "completion_tokens", None),
            )

            choice = resp.choices[0]
            msg = choice.message
            yield EngineEvent(kind="sdk_message", payload=msg)

            tool_calls = list(getattr(msg, "tool_calls", []) or [])
            if not tool_calls:
                final_assistant_text = msg.content or ""
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [_serialise_openai_tool_call(tc) for tc in tool_calls],
                }
            )

            for tc in tool_calls:
                raw_input = _safe_json_load(tc.function.arguments)
                permission_req = PermissionRequest(
                    call_id=tc.id,
                    tool_name=tc.function.name,
                    input=raw_input,
                )
                decision = await ctx.on_permission_request(permission_req)
                if decision.get("behavior") == "deny":
                    tool_output = (
                        f"denied by policy: {decision.get('message', 'no reason supplied')}"
                    )
                else:
                    tool_output = (
                        "tool execution is not built into GitAgentEngine "
                        "in 0.2.0 — wire an MCP server for filesystem tools."
                    )
                yield EngineEvent(
                    kind="sdk_message",
                    payload={
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_output,
                    },
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_output,
                    }
                )

        prior_turns.append({"user": user_msg, "assistant": final_assistant_text})


def _build_openai_client(envs: Mapping[str, str] | None = None) -> Any:
    try:
        import openai  # type: ignore
    except ImportError as exc:
        raise ImportError(_MISSING_OPENAI_HINT) from exc

    base_url = _env(envs, "GITCLAW_MODEL_BASE_URL")
    api_key = _env(envs, "OPENAI_API_KEY")
    if not base_url:
        raise RuntimeError(
            "GitAgentEngine (OpenAI backend) requires GITCLAW_MODEL_BASE_URL — supply it via the "
            "agent's model gateway, ComputerAgent(envs=...), or the process environment."
        )
    if not api_key:
        raise RuntimeError(
            "GitAgentEngine (OpenAI backend) requires OPENAI_API_KEY — supply it via the agent's "
            "model gateway, ComputerAgent(envs=...), or the process environment."
        )
    return openai.AsyncOpenAI(base_url=base_url, api_key=api_key)


def _build_openai_messages(
    system_prompt: str,
    prior_turns: list[dict[str, Any]],
    user_msg: dict[str, Any],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    suffix = _serialize_prior_turns(prior_turns)
    if system_prompt or suffix:
        joined = (system_prompt or "").strip()
        if suffix:
            joined = f"{joined}\n\n{suffix}".strip()
        messages.append({"role": "system", "content": joined})

    content = user_msg.get("content")
    role = user_msg.get("role", "user")
    if isinstance(content, (str, list)):
        messages.append({"role": role, "content": content})
    else:
        messages.append({"role": role, "content": str(content)})
    return messages


def _serialize_prior_turns(prior_turns: list[dict[str, Any]]) -> str:
    if not prior_turns:
        return ""
    lines: list[str] = ["Previous conversation:"]
    for turn in prior_turns:
        user_content = (turn.get("user") or {}).get("content")
        assistant_content = turn.get("assistant", "")
        if user_content is not None:
            lines.append(f"User: {user_content}")
        if assistant_content:
            lines.append(f"Assistant: {assistant_content}")
    return "\n".join(lines)


def _serialise_openai_tool_call(tc: Any) -> dict[str, Any]:
    return {
        "id": tc.id,
        "type": "function",
        "function": {
            "name": tc.function.name,
            "arguments": tc.function.arguments,
        },
    }


def _safe_json_load(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


__all__ = ["GitAgentEngine"]
