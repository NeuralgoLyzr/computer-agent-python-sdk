"""Live end-to-end run of an agent's sub-agent fan-out, executed locally.

Staged so a failure localises itself. Without that, every fan-out failure
presents identically — "the run finished and nothing happened" — which covers a
model that answered in prose instead of composing a roster, a roster the harness
could not parse, workers that all died on a bad model id, a merge that discarded
someone's work, and a closing turn that summarised nothing.

Two modes, because a real orchestrator's behaviour and the harness's plumbing
fail for different reasons and should be diagnosed apart:

    # scripted — deterministic, no model calls, answers "does the wave work"
    uv run python examples/e2e/subagent_live_run.py --scripted

    # live — a real agent composes the roster and real workers do the work
    uv run python examples/e2e/subagent_live_run.py \\
        --base-url http://localhost:8788/agentos/api \\
        --agent-id 6aa93f1b3817a13d3103d2c1 \\
        --api-key "$LYZR_API_KEY" --llm-proxy-url http://127.0.0.1:8786

``--scripted`` drives the harness with a stub engine that emits a fixed roster
and writes a **sentinel** file per worker. That is deliberate: in a fan-out,
"the workers ran" and "the model produced something plausible" are otherwise
indistinguishable, and only one of them means this works.

The workspace is a throwaway git repo built in a temp directory, so the run
never touches the caller's own checkout.

Flags fall back to the same environment variables as ``live_agent_run.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

PASS, FAIL, INFO = "  ✓", "  ✗", "  ·"
_failures: list[str] = []

SENTINEL = "ORBITAL-PANGOLIN-7731"

ROSTER_PROMPT = (
    "Split this into exactly two workers that do not touch the same files. "
    "Worker w1 writes src/alpha.txt containing the word ALPHA; worker w2 writes "
    "docs/beta.md containing the word BETA. Emit the roster JSON block and nothing else."
)


def stage(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def ok(msg: str) -> None:
    print(f"{PASS} {msg}")


def bad(msg: str) -> None:
    _failures.append(msg)
    print(f"{FAIL} {msg}")


def info(msg: str) -> None:
    print(f"{INFO} {msg}")


def make_workspace() -> Path:
    """A throwaway GAP repo for the orchestrator to fan out inside."""
    root = Path(tempfile.mkdtemp(prefix="subagent-e2e-"))
    (root / "agent.yaml").write_text(
        "name: orchestrator\n"
        "version: '1.0.0'\n"
        "description: Splits work across two workers.\n"
        "orchestration:\n"
        "  mode: roster\n"
        "  isolation: worktree\n"
        "  collect: gate\n"
        "  max_parallel: 2\n",
        encoding="utf-8",
    )
    (root / "SOUL.md").write_text(
        "You are an orchestrator. When asked to build something, emit a JSON block\n"
        '```json\n{"workers": [{"id": "...", "task": "...", "scope": {"write": ["..."]}}]}\n```\n'
        "and nothing else.\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("baseline\n", encoding="utf-8")
    return root


# ── the scripted engine ────────────────────────────────────────────────────


def scripted_engine() -> Any:
    """A stub engine: roster on the orchestrator's first turn, a file write for
    each worker, and a closing answer carrying the sentinel."""
    from computeragent.protocol_types import EngineCapabilities, EngineEvent

    roster = json.dumps(
        {
            "workers": [
                {"id": "w1", "task": "write alpha", "scope": {"write": ["src/**"]}},
                {"id": "w2", "task": "write beta", "scope": {"write": ["docs/**"]}},
            ]
        }
    )
    writes = {"write alpha": ("src/alpha.txt", "ALPHA"), "write beta": ("docs/beta.md", "BETA")}

    class Scripted:
        name = "claude-agent-sdk"
        capabilities = EngineCapabilities(
            streaming_input=True,
            partial_messages=True,
            permission_callback=True,
            sessions=True,
            budget=True,
        )

        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def start_session(self, ctx):
            async for entry in ctx.user_message_queue:
                self.prompts.append(str(entry.get("content", "")))
                break
            prompt = self.prompts[-1]
            if prompt in writes:
                rel, body = writes[prompt]
                target = Path(ctx.workspace_path) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"{body}\n", encoding="utf-8")
                text = f"wrote {rel}"
            elif "Wave summary" in prompt or "no worker reports" in prompt:
                text = f"Both workers finished. {SENTINEL}"
            else:
                text = roster
            from fakes.scripted_engine import assistant_message, result_message

            yield EngineEvent(kind="sdk_message", payload=assistant_message(text))
            yield EngineEvent(kind="sdk_message", payload=result_message(text))

    return Scripted()


# ── the run ────────────────────────────────────────────────────────────────


async def run_scripted(workspace: Path) -> int:
    from computeragent.harness.coordinator import HarnessCoordinator
    from computeragent.identity.gap import GapIdentityLoader
    from computeragent.substrates import LocalSubstrate

    engine = scripted_engine()

    stage(1, "Load the agent and read its orchestration block")
    manifest = await GapIdentityLoader().load(
        source=workspace, target_engine="claude-agent-sdk", workspace_path=workspace
    )
    spec = manifest.orchestration
    if spec is None:
        bad("agent.yaml declares `orchestration:` but the loader surfaced no spec")
        return _summary()
    ok(f"mode={spec.mode} isolation={spec.isolation} collect={spec.collect}")

    coordinator = HarnessCoordinator(
        engine=engine,
        substrate=LocalSubstrate(),
        identity_loader=GapIdentityLoader(),
        source=workspace,
        envs={},
        agent_name="orchestrator",
    )

    stage(2, "Run one turn and collect the wave")
    events: list[Any] = []
    try:
        _state, stream = await coordinator.run_chat(ROSTER_PROMPT)
        async for ev in stream:
            events.append(ev)
    except Exception as exc:  # noqa: BLE001 — the point is to report, not raise
        bad(f"the turn raised: {type(exc).__name__}: {exc}")
        await coordinator.dispose()
        return _summary()
    ok(f"{len(events)} events")

    kinds = [e.kind for e in events]
    stage(3, "The roster was parsed and a wave was announced")
    if "worker_wave_started" in kinds:
        planned = next(e for e in events if e.kind == "worker_wave_started").payload["planned"]
        ok(f"planned {[p['id'] for p in planned]}")
    else:
        bad("no wave started — the orchestrator's text did not parse as a roster")

    stage(4, "Both workers ran")
    started = [e for e in events if e.kind == "worker_started"]
    completed = [e for e in events if e.kind == "worker_completed"]
    if len(started) == 2 and len(completed) == 2:
        ok(f"started={[e.payload['worker_id'] for e in started]}")
    else:
        bad(f"expected 2 started + 2 completed, got {len(started)} + {len(completed)}")
    for e in completed:
        status = e.payload["status"]
        if status == "completed":
            ok(f"{e.payload['worker_id']}: {status}, artifacts={e.payload.get('artifacts')}")
        else:
            bad(f"{e.payload['worker_id']}: {status} — {e.payload.get('error')}")

    stage(5, "Each worker's writes were attributed to it, and only to it")
    by_id = {e.payload["worker_id"]: e.payload.get("artifacts") or [] for e in completed}
    if by_id.get("w1") == ["src/alpha.txt"] and by_id.get("w2") == ["docs/beta.md"]:
        ok("per-worker diffs are disjoint")
    else:
        bad(f"artifact attribution is wrong: {by_id}")

    stage(6, "The branches merged with no conflict")
    ended = next((e for e in events if e.kind == "worker_wave_ended"), None)
    if ended is None:
        bad("no wave_ended event")
    else:
        integrated = set(ended.payload.get("integrated") or [])
        conflicts = ended.payload.get("conflicts") or {}
        if integrated == {"src/alpha.txt", "docs/beta.md"}:
            ok(f"integrated {sorted(integrated)}")
        else:
            bad(f"integrated set is wrong: {sorted(integrated)}")
        if conflicts:
            bad(f"unexpected conflicts: {conflicts}")
        else:
            ok("no conflicts — the scopes did not overlap")

    stage(7, "The orchestrator's own manifest survived the wave")
    # Every worker's branch modifying agent.yaml is what used to make any wave
    # of >=2 workers conflict on a file none of them touched.
    if (workspace / "agent.yaml").read_text(encoding="utf-8").startswith("name: orchestrator"):
        ok("agent.yaml is unchanged")
    else:
        bad("agent.yaml was overwritten by a worker's identity")

    stage(8, "The gate was scored")
    card = next((e for e in events if e.kind == "orchestration_scorecard"), None)
    if card is None:
        bad("collect: gate produced no scorecard")
    elif card.payload["verdict"] == "pass":
        ok("verdict=pass")
    else:
        bad(f"verdict={card.payload['verdict']} reasons={card.payload['scorecard']['gate']}")

    stage(9, "The closing turn answered, and the answer is what the run reports")
    digest = engine.prompts[-1] if engine.prompts else ""
    if "Wave summary: 2 dispatched, 2 completed" in digest:
        ok("the closing turn was handed both reports")
    else:
        bad("the closing turn did not receive a wave digest")
    await coordinator.dispose()
    final = coordinator._lifecycle_last_assistant_text  # noqa: SLF001
    if SENTINEL in final:
        ok("the run reports the closing answer, not the roster")
    else:
        bad(f"the run's final text is not the closing answer: {final[:120]!r}")

    return _summary()


async def run_live(args: argparse.Namespace, workspace: Path) -> int:
    from computeragent import ComputerAgent

    stage(1, "Resolve the agent")
    try:
        agent = ComputerAgent(
            base_url=args.base_url,
            agent_id=args.agent_id,
            api_key=args.api_key,
            llm_proxy_url=args.llm_proxy_url,
            source=workspace,
        )
    except Exception as exc:  # noqa: BLE001
        bad(f"resolve failed: {type(exc).__name__}: {exc}")
        return _summary()
    ok(f"{agent.agent.name} (harness={agent.agent.harness})")

    stage(2, "Run one turn against the live model")
    events: list[Any] = []
    try:
        async with agent:
            handle = agent.chat(args.prompt or ROSTER_PROMPT)
            async for ev in handle:
                events.append(ev)
                if ev.kind in {
                    "worker_wave_started",
                    "worker_started",
                    "worker_completed",
                    "worker_wave_ended",
                    "orchestration_scorecard",
                }:
                    info(f"{ev.kind}: {json.dumps(ev.payload, default=str)[:200]}")
            result = await handle.result()
    except Exception as exc:  # noqa: BLE001
        bad(f"the turn raised: {type(exc).__name__}: {exc}")
        return _summary()
    ok(f"session={result.session_id} cost=${result.usage.cost_usd:.4f}")

    kinds = [e.kind for e in events]
    stage(3, "The model composed a roster the harness could dispatch")
    if "worker_started" in kinds:
        ok(f"{kinds.count('worker_started')} worker(s) dispatched")
    elif "worker_wave_started" in kinds:
        bad("a wave was announced but nobody was dispatched — check the roster the model wrote")
    else:
        bad("no wave at all — the agent has no orchestration block, or answered in prose")

    stage(4, "Every worker completed")
    for ev in (e for e in events if e.kind == "worker_completed"):
        if ev.payload["status"] == "completed":
            ok(f"{ev.payload['worker_id']}: artifacts={ev.payload.get('artifacts')}")
        else:
            bad(f"{ev.payload['worker_id']}: {ev.payload.get('error')}")

    stage(5, "The wave merged and the run answered")
    ended = next((e for e in events if e.kind == "worker_wave_ended"), None)
    if ended is not None and ended.payload.get("conflicts"):
        bad(f"scope overlap: {ended.payload['conflicts']}")
    elif ended is not None:
        ok(f"integrated {ended.payload.get('integrated')}")
    return _summary()


def _summary() -> int:
    print("\n" + "─" * 60)
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("All stages passed.")
    return 0


async def main(args: argparse.Namespace) -> int:
    workspace = args.source or make_workspace()
    info(f"workspace: {workspace}")
    try:
        if args.scripted:
            return await run_scripted(workspace)
        return await run_live(args, workspace)
    finally:
        if args.source is None and not args.keep:
            shutil.rmtree(workspace, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--base-url", default=os.environ.get("AGENTOS_BASE_URL"))
    p.add_argument("--agent-id", default=os.environ.get("COMPUTERAGENT_AGENT_ID"))
    p.add_argument("--api-key", default=os.environ.get("LYZR_API_KEY"))
    p.add_argument("--llm-proxy-url", default=os.environ.get("LLM_PROXY_URL"))
    p.add_argument(
        "--scripted",
        action="store_true",
        help="drive the harness with a stub engine — no model calls, no credentials",
    )
    p.add_argument("--prompt", default=None)
    p.add_argument(
        "--source",
        default=None,
        type=lambda v: Path(v).resolve(),
        help="use this workspace instead of building a throwaway one",
    )
    p.add_argument("--keep", action="store_true", help="do not delete the temp workspace")
    args = p.parse_args()
    if not args.scripted:
        missing = [n for n in ("base_url", "agent_id", "api_key") if not getattr(args, n)]
        if missing:
            p.error(
                "missing required: "
                + ", ".join(f"--{n.replace('_', '-')}" for n in missing)
                + " (or pass --scripted)"
            )
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
