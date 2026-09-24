"""Live end-to-end run of an agent's MCP tools, executed locally.

Staged so a failure localises itself. Without that, every MCP failure presents
identically — "the agent didn't use the tool" — which covers a dead facade, a
mismatched action name, a namespace the CLI cannot address, a model that simply
chose not to, and a tool whose result the model read as an error.

Two modes, because the real service cannot be the routine substrate: it is
remote and cannot reach a locally-hosted MCP server, so it can only proxy tools
that already exist in an account.

    # contract — runnable anywhere, deterministic, no MCP credentials
    uv run python examples/e2e/mcp_live_run.py --fake-facade \\
        --base-url http://localhost:8788/agentos/api \\
        --agent-id 6aa93f1b3817a13d3103d2c1 \\
        --api-key "$LYZR_API_KEY" --llm-proxy-url http://127.0.0.1:8786

    # live — answers "does OUR key work against the real facade"
    uv run python examples/e2e/mcp_live_run.py --mcp-base-url https://mcp.lyzr.ai ...

With ``--fake-facade`` the agent's bindings are replaced with one pointing at a
local server whose tool returns a **sentinel**. That is deliberate: in an agent
end-to-end run, "the tool returned data" and "the model produced something
plausible" are otherwise indistinguishable, and only one of them means this
works.

Flags fall back to the same environment variables as ``live_agent_run.py``,
plus ``LYZR_MCP_CLIENT_URL`` for the MCP base.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

from computeragent import ComputerAgent  # noqa: E402
from computeragent.mcp.binding import McpBinding  # noqa: E402

PASS, FAIL, INFO = "  ✓", "  ✗", "  ·"
_failures: list[str] = []

FAKE_BINDING_SERVER = "srv_fake"
FAKE_NAMESPACE = "Project"


def stage(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def ok(msg: str) -> None:
    print(f"{PASS} {msg}")


def bad(msg: str) -> None:
    _failures.append(msg)
    print(f"{FAIL} {msg}")


def info(msg: str) -> None:
    print(f"{INFO} {msg}")


def _field(block: Any, key: str) -> Any:
    """One field off a content block, whether it arrived as a dataclass or a dict."""
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


def tool_uses_in(messages: tuple[Any, ...]) -> list[tuple[str, Any]]:
    """Every (tool_name, tool_result) pair the turn produced.

    Detected by SHAPE, not by a `type` discriminator: the SDK's `ToolUseBlock`
    and `ToolResultBlock` are plain dataclasses with fields `(id, name, input)`
    and `(tool_use_id, content, is_error)` and no `type` field at all. Branching
    on `type` silently matches nothing, which reads exactly like a model that
    declined to use its tools.
    """
    uses: dict[str, str] = {}
    out: list[tuple[str, Any]] = []
    for m in messages:
        blocks = getattr(m, "content", None)
        if blocks is None and isinstance(m, dict):
            blocks = (m.get("message") or {}).get("content") or m.get("content")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            name, bid = _field(block, "name"), _field(block, "id")
            if name and bid:
                uses[str(bid)] = str(name)
                continue
            tid = _field(block, "tool_use_id")
            if tid:
                out.append((uses.get(str(tid), "?"), block))
    return out


def result_text(block: Any) -> str:
    return json.dumps(_field(block, "content"), default=str)


def is_error(block: Any) -> bool:
    return bool(_field(block, "is_error"))


async def main(args: argparse.Namespace) -> int:
    facade_cm: Any = contextlib.nullcontext()
    sentinel = None
    if args.fake_facade:
        from fakes.lyzr_mcp_facade import FakeLyzrMcpFacade

        facade_cm = FakeLyzrMcpFacade()

    with facade_cm as facade:
        mcp_base = facade.base_url if facade is not None else args.mcp_base_url
        if facade is not None:
            sentinel = facade.sentinel
            info(f"fake facade on {mcp_base}, sentinel={sentinel}")

        stage(1, "Resolve the agent and its MCP bindings")
        try:
            agent = ComputerAgent(
                base_url=args.base_url,
                agent_id=args.agent_id,
                api_key=args.api_key,
                llm_proxy_url=args.llm_proxy_url,
                engine="claude-agent-sdk",
                mcp_base_url=mcp_base,
                source=args.source or Path.cwd(),
                options={"system_prompt": "You are terse. Use your tools when they apply."},
                policy_decider=_deny_mcp if args.deny_mcp else None,
            )
        except Exception as exc:
            bad(f"{type(exc).__name__}: {exc}")
            return _summary()

        if facade is not None:
            # The registered agent almost certainly binds nothing, so give it a
            # binding pointing at the fake. Everything downstream is untouched.
            binding = McpBinding(
                server_id=FAKE_BINDING_SERVER,
                action_names=("lookup_sentinel",),
                tool_name=FAKE_NAMESPACE,
                persist_auth=False,
            )
            agent._coordinator._mcp_bindings = (binding,)
            agent._coordinator._mcp_client = agent._build_mcp_client() or _client(mcp_base, args)
            info("injected one binding at the fake facade")

        bindings = agent._coordinator._mcp_bindings
        if bindings:
            ok(f"bindings: {', '.join(f'{b.tool_name}({b.server_id})' for b in bindings)}")
            ok(f"mcp_mode={agent.agent.mcp_mode}")
        else:
            bad(
                "the agent binds no MCP servers, so there is nothing to test. Bind one in "
                "AgentOS, or run with --fake-facade."
            )
            return _summary()

        stage(2, "Preflight the MCP facade")
        # Three separate messages, because from inside the SDK a dead base, a
        # rejected key and an unknown server id all present later as "no tools".
        client = agent._coordinator._mcp_client
        for b in bindings:
            try:
                every = await client.list_tools(b.server_id)
            except Exception as exc:
                bad(
                    f"{b.server_id}: cannot reach the facade at {mcp_base} "
                    f"({type(exc).__name__}: {exc}). Check --mcp-base-url and the key."
                )
                continue
            names = [t.name for t in every]
            matched = [t for t in names if t in b.action_names]
            if matched:
                ok(f"{b.server_id}: {len(matched)}/{len(b.action_names)} bound tools exist")
            else:
                bad(
                    f"{b.server_id}: none of the bound action_names exist on the server. "
                    f"Bound: {', '.join(b.action_names)}. Server has: {', '.join(names) or '(none)'}. "
                    "These must match EXACTLY."
                )

        stage(3, "Preflight the model path")
        resolved = agent.agent
        if resolved.has_native_gateway:
            import httpx

            async with httpx.AsyncClient(timeout=15) as probe:
                try:
                    r = await probe.get(f"{args.llm_proxy_url.rstrip('/')}/health")
                    ok(f"proxy alive -> {r.status_code}")
                except Exception as exc:
                    bad(
                        f"proxy unreachable ({type(exc).__name__}) — start it or pass --llm-proxy-url"
                    )
                try:
                    await probe.get(resolved.gateway_base_url)
                    ok(f"gateway reachable: {resolved.gateway_base_url}")
                except Exception as exc:
                    bad(
                        f"gateway unreachable: {resolved.gateway_base_url} ({type(exc).__name__}). "
                        "The SDK is configured correctly but has nowhere to send model calls."
                    )
        else:
            info("no gateway — direct Anthropic")

        stage(4, "Build the in-process MCP servers")
        prompt = args.prompt or (
            "Call your lookup_sentinel tool and reply with exactly the sentinel it returns."
            if facade is not None
            else "List the tools you have, then use the most relevant one and report its result."
        )
        handle = agent.chat(prompt)
        try:
            result = await handle.result()
        except Exception as exc:
            bad(f"the turn raised: {type(exc).__name__}: {exc}")
            await agent.dispose()
            return _summary()

        options = agent._coordinator._manifest.options or {}
        servers = options.get("mcp_servers") or {}
        if servers:
            ok(f"mcp_servers: {', '.join(servers)}")
        else:
            bad("no MCP servers were attached — see the WARNINGs above for which binding and why")

        stage(5, "Options reached the engine intact")
        allowed = options.get("allowed_tools")
        if allowed is None:
            ok("allowed_tools absent — every tool stays available (correct: never CREATE one)")
        else:
            ok(f"allowed_tools extended: {', '.join(allowed)}")
        if "gap_tools" in servers or len(servers) > 1:
            ok("loader-supplied servers coexist with the agent's bindings")

        stage(6, "The model called an MCP tool")
        calls = tool_uses_in(result.messages)
        mcp_calls = [(n, b) for n, b in calls if n.startswith("mcp__")]
        if mcp_calls:
            ok(f"MCP tools called: {', '.join(sorted({n for n, _ in mcp_calls}))}")
        elif args.deny_mcp:
            ok("no MCP call completed — expected, the policy denied it")
        else:
            bad(
                "no MCP tool call observed. Either the model saw the tools and chose not to use "
                f"them (tools it did use: {', '.join(n for n, _ in calls) or 'none'}), or stage 4 "
                "attached nothing."
            )

        stage(7, "The result came back well-formed")
        # THE property: a successful upstream call must not reach the model as
        # an error. That is the `invalid_union` failure the content layer exists
        # to prevent, and it is invisible from anywhere but here.
        for name, block in mcp_calls:
            if is_error(block) and not args.deny_mcp:
                bad(f"{name} returned is_error — the tool ran but its result was rejected")
            elif not args.deny_mcp:
                ok(f"{name} returned a clean result")
        if sentinel is not None and not args.deny_mcp:
            blob = " ".join(result_text(b) for _, b in mcp_calls)
            reply = json.dumps([getattr(m, "content", None) for m in result.messages], default=str)
            if sentinel in blob:
                ok("the sentinel reached the model through the tool result")
            else:
                bad("the sentinel is NOT in any tool result — the content path dropped it")
            if sentinel in reply:
                ok("the model echoed the sentinel — it genuinely read the tool output")
            else:
                info("the model did not echo the sentinel (it may have paraphrased)")

        stage(8, "Governance")
        if args.deny_mcp:
            denied = [b for _, b in mcp_calls if is_error(b)]
            if denied or not mcp_calls:
                ok("the policy blocked the MCP call under bypassPermissions")
            else:
                bad(
                    "--deny-mcp was set but an MCP call completed — the PreToolUse gate did not fire"
                )
            # An is_error result the model can read is NOT proof of enforcement:
            # the tool could have run and its output been discarded afterwards.
            # Enforcement means the request never reached the facade at all, and
            # only the facade can say so.
            if facade is not None:
                executed = [r for r in facade.requests if r[1].endswith("/tools/execute")]
                if executed:
                    bad(
                        f"the policy denied the call but the facade still executed it "
                        f"({len(executed)} request(s)) — the gate ran too late to prevent anything"
                    )
                else:
                    ok("the facade received no execute request — the call was stopped, not undone")
        else:
            info("re-run with --deny-mcp to exercise the policy gate")

        stage(9, "A second turn still has the tools")
        await agent.chat("Name one tool you have available.")
        again = (agent._coordinator._manifest.options or {}).get("mcp_servers") or {}
        if set(again) == set(servers):
            ok("the servers survived the resumed turn")
        else:
            bad(f"servers changed across turns: {set(servers)} -> {set(again)}")

        await agent.dispose()
    return _summary()


def _client(base: str, args: argparse.Namespace) -> Any:
    from computeragent.mcp.client import LyzrMcpClient

    return LyzrMcpClient(base_url=base, api_key=args.api_key)


async def _deny_mcp(tool_name: str, _input: Any, _context: Any) -> dict[str, Any]:
    if tool_name.startswith("mcp__"):
        return {"behavior": "deny", "message": f"{tool_name} denied by --deny-mcp"}
    return {"behavior": "allow"}


def _summary() -> int:
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
    p.add_argument("--mcp-base-url", default=os.environ.get("LYZR_MCP_CLIENT_URL"))
    p.add_argument(
        "--fake-facade",
        action="store_true",
        help="serve the facade locally and inject a binding at it (no MCP credentials needed)",
    )
    p.add_argument("--deny-mcp", action="store_true", help="exercise the policy gate")
    p.add_argument("--prompt", default=None)
    p.add_argument("--source", default=None, type=lambda v: Path(v).resolve())
    args = p.parse_args()
    missing = [n for n in ("base_url", "agent_id", "api_key") if not getattr(args, n)]
    if missing:
        p.error("missing required: " + ", ".join(f"--{n.replace('_', '-')}" for n in missing))
    if not args.fake_facade and not args.mcp_base_url:
        p.error("pass --mcp-base-url (or set LYZR_MCP_CLIENT_URL), or use --fake-facade")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
