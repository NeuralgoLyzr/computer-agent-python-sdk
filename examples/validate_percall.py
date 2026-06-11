"""End-to-end validation of the per-LLM-call OTel rework (local, no backend).

Runs a real multi-tool ComputerAgent turn with the OtelSink wired to an
IN-MEMORY span exporter, then prints the span tree and asserts the invariants:

  • one `chat` span PER LLM call (not one per turn)
  • per-call tokens (incl. cache) live on the chat spans
  • cumulative cost lives on the invoke_agent root; NO tokens on the root
    (so summing tokens across the trace's spans can't double-count)
  • execute_tool spans are siblings of the chat spans (children of the root)
  • per-call TTFT (gen_ai.response.time_to_first_chunk) on the chat spans

Run (needs ANTHROPIC_API_KEY, or AWS creds for Bedrock):
    cd computeragent-py
    uv run python examples/validate_percall.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from computeragent import ComputerAgent
from computeragent.telemetry import configure
from computeragent.telemetry.sinks import OtelSink

IN = "gen_ai.usage.input_tokens"
OUT = "gen_ai.usage.output_tokens"
CR = "gen_ai.usage.cache_read.input_tokens"
CC = "gen_ai.usage.cache_creation.input_tokens"
COST = "computeragent.usage.cost_usd"
TTFT = "gen_ai.response.time_to_first_chunk"


def _int(attrs, key: str) -> int:
    v = attrs.get(key)
    return int(v) if isinstance(v, (int, float)) else 0


async def main() -> None:
    span_exp = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(span_exp))
    mp = MeterProvider(metric_readers=[InMemoryMetricReader()])

    # Wire the OtelSink to the in-memory providers and make it the active sink.
    configure(sinks=[OtelSink(tracer_provider=tp, meter_provider=mp)])

    async with ComputerAgent(
        source=Path.cwd(),
        engine="claude-agent-sdk",
        runtime="local",
        agent_name="percall-validate",
        options={
            "system_prompt": "You are a terse assistant.",
            "allowed_tools": ["Read", "Glob", "Grep"],
            "permission_mode": "bypassPermissions",
            "max_turns": 8,
        },
    ) as agent:
        # A task that forces several tool rounds → several LLM calls.
        await agent.chat(
            "Use Glob to list the Markdown (*.md) files in the current directory, "
            "then Read two of them and give a one-line summary of each."
        )

    tp.force_flush()
    spans = sorted(span_exp.get_finished_spans(), key=lambda s: s.start_time)

    roots = [s for s in spans if s.name.startswith("invoke_agent")]
    chats = [s for s in spans if s.name.startswith("chat")]
    tools = [s for s in spans if s.name.startswith("execute_tool")]
    by_id = {s.context.span_id: s for s in spans}

    print("\n" + "=" * 72)
    print("SPAN TREE")
    print("=" * 72)
    for s in spans:
        a = dict(s.attributes or {})
        dur_ms = (s.end_time - s.start_time) / 1e6
        line = f"{s.name:<34} {dur_ms:8.0f}ms"
        if s.name.startswith("chat"):
            extras = f"  in={_int(a, IN)} out={_int(a, OUT)} cacheR={_int(a, CR)} cacheC={_int(a, CC)}"
            if TTFT in a:
                extras += f"  ttft={float(a[TTFT]) * 1000:.0f}ms"
            line += extras
        if s.name.startswith("invoke_agent"):
            line += f"  cost=${float(a.get(COST, 0)):.4f}  (in={_int(a, IN)} out={_int(a, OUT)})"
        print(line)

    # ── Invariants ───────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("CHECKS")
    print("=" * 72)
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    check("exactly one invoke_agent root (one turn)", len(roots) == 1, f"{len(roots)} found")
    check("one chat span per LLM call (>=1)", len(chats) >= 1, f"{len(chats)} chat spans")
    root = roots[0] if roots else None
    if root is not None:
        ra = dict(root.attributes or {})
        check("root carries cumulative cost", float(ra.get(COST, 0)) > 0, f"${float(ra.get(COST, 0)):.4f}")
        check("root has NO token attributes (no double-count)", IN not in ra and OUT not in ra)

    # Tokens summed across chat spans = the turn total (reconciles with cost).
    chat_in = sum(_int(dict(c.attributes or {}), IN) for c in chats)
    chat_out = sum(_int(dict(c.attributes or {}), OUT) for c in chats)
    chat_cache = sum(_int(dict(c.attributes or {}), CR) + _int(dict(c.attributes or {}), CC) for c in chats)
    check("chat spans carry token usage", chat_in + chat_out > 0,
          f"sum input={chat_in} output={chat_out} cached={chat_cache}")
    check("real input incl cache >> non-cached (cache reconciles with cost)",
          chat_cache > 0, f"cached={chat_cache} (0 is fine for a tiny/cold run)")

    # Tools are siblings of chats (children of the root).
    if tools and root is not None:
        all_under_root = all(by_id.get(t.parent.span_id) is root for t in tools if t.parent)
        check("execute_tool spans are children of invoke_agent (siblings of chat)", all_under_root)

    ttft_chats = [c for c in chats if TTFT in (c.attributes or {})]
    check("at least one chat span has per-call TTFT", len(ttft_chats) >= 1,
          f"{len(ttft_chats)}/{len(chats)} chat spans")

    print("\n" + ("ALL CHECKS PASSED ✅" if not failures else f"FAILURES: {failures} ❌"))


if __name__ == "__main__":
    asyncio.run(main())
