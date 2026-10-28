"""Live end-to-end run of an AgentOS-registered agent, executed locally.

Every stage prints PASS/FAIL as it happens, so a failure localises itself
instead of arriving as "it didn't work". The stages are ordered by dependency:
a later one cannot pass if an earlier one failed.

ComputerAgent reads every setting from its environment variable, so the run is
configured the way a sandbox is:

    export COMPUTER_AGENT_ID=6aa93f1b3817a13d3103d2c1
    export COMPUTER_AGENT_ENGINE=claude-agent-sdk
    export COMPUTER_AGENT_EGRESS_API_URL=http://egress/agentos/api
    export COMPUTER_AGENT_EGRESS_GUARDRAILS_URL=http://egress/guardrails
    export COMPUTER_AGENT_EGRESS_LLM_URL=http://egress/llm
    export COMPUTER_AGENT_EGRESS_MCP_URL=http://egress/mcp/v1
    export COMPUTER_AGENT_EGRESS_PROXY_SECRET=...
    # optional: COMPUTER_AGENT_EGRESS_TELEMETRY_URL, COMPUTER_AGENT_GAP_DIR

    uv run python examples/e2e/live_agent_run.py

``--gap-dir`` and ``--engine`` override their variables.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from computeragent import ComputerAgent

PASS, FAIL, INFO = "  ✓", "  ✗", "  ·"
_failures: list[str] = []


def stage(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def ok(msg: str) -> None:
    print(f"{PASS} {msg}")


def bad(msg: str) -> None:
    _failures.append(msg)
    print(f"{FAIL} {msg}")


def info(msg: str) -> None:
    print(f"{INFO} {msg}")


def reply_text(messages: tuple[Any, ...]) -> str:
    """The assistant's final text, across the shapes an engine may yield."""
    for m in reversed(messages):
        if isinstance(m, dict):
            if isinstance(m.get("result"), str) and m.get("type") == "result":
                return m["result"]
            content = (
                (m.get("message") or {}).get("content") if m.get("type") == "assistant" else None
            )
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                text = "".join(b.get("text", "") for b in content if isinstance(b, dict))
                if text:
                    return text
        text = getattr(m, "result", None)
        if isinstance(text, str) and text:
            return text
        blocks = getattr(m, "content", None)
        if isinstance(blocks, list):
            text = "".join(getattr(b, "text", "") or "" for b in blocks)
            if text:
                return text
    return ""


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}"


async def main(args: argparse.Namespace) -> int:
    stage(1, "Resolve the agent from AgentOS (base_url)")
    try:
        agent = ComputerAgent(
            engine=args.engine,
            gap_dir=args.gap_dir,
            # NOT `allowed_tools`: the dispatch permission mode is
            # `bypassPermissions`, where an allowlist restricts nothing.
            options={"system_prompt": "You are terse.", "disallowed_tools": ["Write", "Edit"]},
        )
    except Exception as exc:
        bad(f"{type(exc).__name__}: {exc}")
        return 1

    resolved = agent.agent
    settings = agent._settings  # noqa: SLF001 — diagnostics, not API
    ok(f"name={resolved.name!r} harness={resolved.harness} model={resolved.model}")
    ok(f"MCP bindings={[b.tool_name for b in resolved.mcp_bindings]}")

    stage(2, "The egress routes")
    routes = {
        "base_url": settings.base_url,
        "guardrails_url": settings.guardrails_url,
        "llm_url": settings.llm_url,
        "mcp_url": settings.mcp_url,
        "telemetry_url": settings.telemetry_url,
    }
    for name, url in routes.items():
        if url:
            ok(f"{name:14} {url}")
        else:
            info(f"{name:14} unset (off)")
    envs = agent._coordinator._envs  # noqa: SLF001
    if envs.get("ANTHROPIC_BASE_URL") == settings.llm_url:
        ok("model calls go to llm_url")
    else:
        bad(f"ANTHROPIC_BASE_URL is {envs.get('ANTHROPIC_BASE_URL')!r}, expected llm_url")

    import httpx

    async with httpx.AsyncClient(timeout=10) as probe:
        for name, url in routes.items():
            if not url:
                continue
            try:
                r = await probe.get(origin(url))
                ok(f"{name} reachable: {origin(url)} -> {r.status_code}")
            except Exception as exc:
                bad(f"{name} unreachable at {origin(url)} ({type(exc).__name__})")

    stage(3, "The guardrails check answers (guardrails_url /v1/check)")
    from computeragent.policy.guardrails_client import GuardrailsUnavailable, check_text

    try:
        verdict = await check_text(
            settings.guardrails_url, api_key=settings.api_key(), text="hello"
        )
        ok(f"outcome={verdict.outcome} skipped={verdict.skipped}")
    except GuardrailsUnavailable as exc:
        bad(f"no verdict ({exc}) — every turn would block")

    stage(4, "Load the identity")
    coordinator = agent._coordinator  # noqa: SLF001
    try:
        await coordinator._ensure_booted()  # noqa: SLF001
    except Exception as exc:
        bad(f"boot failed — {type(exc).__name__}: {exc}")
        await agent.dispose()
        return 1
    manifest = coordinator._manifest  # noqa: SLF001
    ok(f"workspace={coordinator._workspace_path} manifest={manifest.name!r}")  # noqa: SLF001
    effective_model = (manifest.options or {}).get("model")
    if effective_model == resolved.model:
        ok(f"model survived the loader: {effective_model}")
    else:
        bad(f"model is {effective_model!r}, expected {resolved.model!r}")

    stage(5, "Run one turn against the live model")
    prompt = "List the files in your current working directory, then say how many there are."
    tool_uses: list[str] = []
    try:
        handle = agent.chat(prompt)
        async for event in handle:
            payload = getattr(event, "payload", None)
            blocks = getattr(payload, "content", None)
            if isinstance(blocks, list):
                for block in blocks:
                    name = getattr(block, "name", None)
                    if name:
                        tool_uses.append(name)
        result = await handle
    except Exception as exc:
        bad(f"the turn failed — {type(exc).__name__}: {exc}")
        await agent.dispose()
        return 1

    ok(f"session={result.session_id}")
    ok(f"tokens in={result.usage.input_tokens} out={result.usage.output_tokens}")
    ok(f"duration={result.duration_ms:.0f}ms")

    stage(6, "Tool calls round-tripped (each one checked by the guardrails)")
    if tool_uses:
        ok(f"tools used: {', '.join(sorted(set(tool_uses)))}")
    else:
        bad("no tool call observed — the agent answered without touching the workspace")

    stage(7, "A non-empty reply came back")
    text = reply_text(result.messages)
    if text.strip():
        ok(f"reply: {text.strip()[:300]}")
    else:
        bad("empty completion")

    await agent.dispose()

    print("\n" + "─" * 60)
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("All stages passed.")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--engine",
        default=None,
        help="'claude-agent-sdk' or 'gitagent' (else COMPUTER_AGENT_ENGINE)",
    )
    p.add_argument(
        "--gap-dir",
        default=None,
        type=lambda v: Path(v).resolve(),
        help="run in GAP mode from this directory (else COMPUTER_AGENT_GAP_DIR)",
    )
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
