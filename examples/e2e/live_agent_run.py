"""Live end-to-end run of an AgentOS-registered agent, executed locally.

Every stage prints PASS/FAIL as it happens, so a failure localises itself
instead of arriving as "it didn't work". The stages are ordered by dependency:
a later one cannot pass if an earlier one failed.

    uv run python examples/e2e/live_agent_run.py \\
        --base-url  http://localhost:8788/agentos/api \\
        --agent-id  6aa93f1b3817a13d3103d2c1 \\
        --api-key   "$LYZR_API_KEY" \\
        --llm-proxy-url http://127.0.0.1:8786

Every flag falls back to an environment variable, so nothing secret has to sit
in shell history: ``AGENTOS_BASE_URL``, ``COMPUTERAGENT_AGENT_ID``,
``LYZR_API_KEY``, ``LLM_PROXY_URL``, ``GIT_TOKEN``.

``--source`` runs the agent against a local directory instead of cloning its
registered source — useful when the source is private and no token is to hand.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

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


async def main(args: argparse.Namespace) -> int:
    source = args.source

    stage(1, "Resolve the agent from AgentOS")
    try:
        agent = ComputerAgent(
            base_url=args.base_url,
            agent_id=args.agent_id,
            api_key=args.api_key,
            llm_proxy_url=args.llm_proxy_url,
            git_token=args.git_token,
            source=source,
            engine=args.engine,
            # NOT `allowed_tools`: the dispatch permission mode is
            # `bypassPermissions`, where an allowlist restricts nothing.
            # `disallowed_tools` is the lever that works.
            options={"system_prompt": "You are terse.", "disallowed_tools": ["Write", "Edit"]},
        )
    except Exception as exc:
        bad(f"{type(exc).__name__}: {exc}")
        return 1

    resolved = agent.agent
    ok(f"name={resolved.name!r} harness={resolved.harness}")
    ok(f"model={resolved.model}  (this is what reaches the gateway)")
    ok(f"gateway={resolved.gateway_type} {resolved.gateway_base_url}")
    if resolved.credential_id:
        info(f"credential_id={resolved.credential_id} — its source is private")

    coordinator = agent._coordinator  # noqa: SLF001 — diagnostics, not API
    envs = coordinator._envs  # noqa: SLF001
    base = envs.get("ANTHROPIC_BASE_URL", "(unset)")
    ok(f"ANTHROPIC_BASE_URL={base}")
    if resolved.has_native_gateway and "/up/" not in base:
        bad("a native gateway must route through the proxy's /up/<b64> segment")

    stage(2, "Preflight the model path")
    # Stage 4 can only fail in ways that look identical from inside the SDK —
    # a dead gateway, a stopped proxy and a bad key all surface as the CLI
    # returning an error result. Two cheap probes here separate them, and this
    # is not hypothetical: these gateways are often cloudflare tunnels whose
    # hostnames go stale, leaving a perfectly correct SDK pointed at NXDOMAIN.
    if resolved.has_native_gateway:
        import httpx

        proxy_health = f"{args.llm_proxy_url.rstrip('/')}/health"
        async with httpx.AsyncClient(timeout=15) as probe:
            try:
                r = await probe.get(proxy_health)
                ok(f"proxy alive: {proxy_health} -> {r.status_code}")
            except Exception as exc:
                bad(
                    f"proxy unreachable at {proxy_health} ({type(exc).__name__}) — "
                    "start it, or pass --llm-proxy-url"
                )
            try:
                await probe.get(resolved.gateway_base_url)
                ok(f"gateway reachable: {resolved.gateway_base_url}")
            except Exception as exc:
                bad(
                    f"gateway unreachable: {resolved.gateway_base_url} ({type(exc).__name__}). "
                    "The SDK is configured correctly but has nowhere to send model calls — the "
                    "gateway's baseUrl in AgentOS is stale. Fix it there, not here."
                )

    stage(3, "Materialise the workspace")
    try:
        await coordinator._ensure_booted()  # noqa: SLF001
    except Exception as exc:
        bad(f"boot failed — {type(exc).__name__}: {exc}")
        await agent.dispose()
        return 1
    workspace = coordinator._workspace_path  # noqa: SLF001
    ok(f"workspace={workspace}")
    if source is None:
        ok("cloned the agent's registered source")

    stage(4, "Load the identity")
    manifest = coordinator._manifest  # noqa: SLF001
    ok(f"manifest name={manifest.name!r} version={manifest.version}")
    effective_model = (manifest.options or {}).get("model")
    if effective_model == resolved.model:
        ok(f"model override survived the loader: {effective_model}")
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
    if result.usage.output_tokens and not result.usage.input_tokens:
        # Not an SDK fault and not worth chasing from this side: the gateway's
        # STREAMING usage omits prompt tokens (the same call made
        # non-streaming reports them), so the proxy has nothing to map onto
        # `input_tokens`. It does mean per-turn cost is understated.
        info(
            "input tokens are 0 while output is not — the gateway omits prompt tokens when streaming"
        )

    stage(6, "Tool calls round-tripped")
    if tool_uses:
        ok(f"tools used: {', '.join(sorted(set(tool_uses)))}")
    else:
        bad("no tool call observed — the agent answered without touching the workspace")

    stage(7, "A non-empty reply came back")
    text = reply_text(result.messages)
    if text.strip():
        ok(f"reply: {text.strip()[:300]}")
    else:
        bad(
            "empty completion. Usual causes: CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS missing, or "
            "the proxy ordering a text block ahead of a tool_result"
        )

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
    p.add_argument("--base-url", default=os.environ.get("AGENTOS_BASE_URL"))
    p.add_argument("--agent-id", default=os.environ.get("COMPUTERAGENT_AGENT_ID"))
    p.add_argument("--api-key", default=os.environ.get("LYZR_API_KEY"))
    p.add_argument("--llm-proxy-url", default=os.environ.get("LLM_PROXY_URL"))
    p.add_argument("--git-token", default=os.environ.get("GIT_TOKEN"))
    p.add_argument(
        "--source",
        default=None,
        type=lambda v: Path(v).resolve(),
        help="run against this directory instead of cloning",
    )
    p.add_argument("--engine", default=None, help="override the agent's harness")
    args = p.parse_args()
    missing = [n for n in ("base_url", "agent_id", "api_key") if not getattr(args, n)]
    if missing:
        p.error("missing required: " + ", ".join(f"--{n.replace('_', '-')}" for n in missing))
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
