"""Live E2E — ComputerAgent(harness_url=...) against a REAL TypeScript harness server.

Boots a real harness server from the sibling monorepo, then runs a 1-turn
``ComputerAgent`` chat over HTTP using the local (editable) computeragent-py
remote client, and asserts the protocol round-trips end to end.

Two modes:
  --protocol  Bare harness protocol — boots examples/harness-server.ts (:7700),
              ComputerAgent(harness_kind="protocol").
  --server    ComputerAgent server — boots examples/computeragent-server.ts (:8787)
              with a LocalSubstrate, ComputerAgent(harness_kind="server", runtime="local").

Requires ANTHROPIC_API_KEY (read from ../ComputerAgent/.env). Needs the TS
workspace built enough to run the example (``pnpm build`` in ../ComputerAgent).

Run:
    cd computeragent-py
    uv run python examples/e2e/remote_harness_live.py --protocol
    uv run python examples/e2e/remote_harness_live.py --server
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

HERE = Path(__file__).resolve()
PY_REPO = HERE.parents[2]
WORKSPACE = PY_REPO.parent
TS_REPO = WORKSPACE / "ComputerAgent"

load_dotenv(TS_REPO / ".env", override=False)
os.environ.setdefault("COMPUTERAGENT_OTEL", "disabled")

MARKER = "REMOTE_LIVE_OK"
PROBE = "probe-remote"

INLINE_SOURCE = {
    "type": "inline",
    "manifest": {"name": "remote-smoke", "version": "0.1.0"},
    "files": {
        "agent.yaml": (
            'spec_version: "0.1.0"\n'
            "name: remote-smoke\n"
            "version: 0.1.0\n"
            "model:\n"
            "  preferred: claude-sonnet-4-5\n"
            "runtime:\n"
            "  permission_mode: bypassPermissions\n"
            "  max_turns: 2\n"
        ),
        "CLAUDE.md": "Reply in one short sentence.",
    },
}


def _log(m: str) -> None:
    print(m, flush=True)


def _fail(m: str) -> int:
    print(f"\nFAIL: {m}", file=sys.stderr, flush=True)
    return 1


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def boot_server(entry: str, *, port: int, log_path: Path) -> subprocess.Popen:
    env = {**os.environ, "PORT": str(port), "HOST": "127.0.0.1", "COMPUTERAGENT_OTEL": "disabled"}
    env.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)  # no OTel for the smoke
    logf = open(log_path, "w")  # noqa: SIM115
    # The TS examples self-run; type-strip the entry with Node (no tsx dep).
    proc = subprocess.Popen(
        ["node", "--experimental-strip-types", entry],
        cwd=str(TS_REPO),
        env=env,
        stdout=logf,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._logf = logf  # type: ignore[attr-defined]
    return proc


def wait_for_health(base: str, health_path: str, *, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base}{health_path}", timeout=3.0)
            if r.status_code == 200:
                return
            last = f"status={r.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = repr(exc)
        time.sleep(0.5)
    raise TimeoutError(f"server health {health_path} not ready within {timeout}s (last: {last})")


def teardown(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    finally:
        lf = getattr(proc, "_logf", None)
        if lf:
            lf.close()


def _tail(path: Path, n: int = 30) -> str:
    with contextlib.suppress(Exception):
        return "\n".join(path.read_text().splitlines()[-n:])
    return ""


async def main() -> int:
    mode = "server" if "--server" in sys.argv else "protocol"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _fail("ANTHROPIC_API_KEY not set (expected in ../ComputerAgent/.env)")

    if mode == "server":
        entry, health_path, kind, runtime = "examples/computeragent-server.ts", "/health", "server", "local"
    else:
        entry, health_path, kind, runtime = "examples/harness-server.ts", "/v1/health", "protocol", None

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = Path(f"/tmp/remote-harness-{mode}-{port}.log")

    import computeragent
    from computeragent import ComputerAgent

    _log(f"→ mode        : {mode}")
    _log(f"→ package     : {computeragent.__file__}")
    _log(f"→ server entry: {entry}  → {base}")
    _log("─" * 60)

    proc = boot_server(entry, port=port, log_path=log_path)
    try:
        _log("→ booting TS harness server …")
        wait_for_health(base, health_path)
        _log("→ server healthy")

        prompt = f"Without using any tool, reply with exactly the text {MARKER} and nothing else. (probe={PROBE})"
        kwargs: dict = dict(
            harness_url=base,
            harness_kind=kind,
            harness="claude-agent-sdk",
            source=INLINE_SOURCE,
            model="claude-sonnet-4-5",
        )
        if runtime:
            kwargs["runtime"] = runtime

        _log(f"→ ComputerAgent(harness_url=…, harness_kind={kind!r}{', runtime=' + repr(runtime) if runtime else ''}) …")
        kinds: list[str] = []
        session_id = ""
        async with ComputerAgent(**kwargs) as agent:
            handle = agent.chat(prompt)
            async for event in handle:
                kinds.append(event.kind)
                if event.kind == "ca_session_started":
                    session_id = getattr(event, "session_id", "") or session_id
                elif event.kind == "ca_session_ended" and getattr(event, "reason", "") == "error":
                    return _fail(f"harness session error: {getattr(event, 'error_message', '')}")
            result = await handle.result()

        # ── assertions ──
        _log("\n─── result ───")
        _log(f"  event kinds   : {kinds}")
        _log(f"  messages      : {len(result.messages)}")
        _log(f"  cost_usd      : {result.usage.cost_usd}")
        _log(f"  session_id    : {session_id or result.session_id}")

        text = _extract_text(result.messages)
        _log(f"  answer        : {text[:80]!r}")

        assert "ca_session_started" in kinds, f"no ca_session_started; got {kinds}"
        assert any(k in ("ca_session_ended", "ca_done") for k in kinds), f"no terminal event; got {kinds}"
        assert MARKER in text, f"marker {MARKER!r} not in answer: {text[:200]!r}"
        assert result.usage.cost_usd and result.usage.cost_usd > 0, "no cost recorded"
        assert (session_id or result.session_id), "no session id"

        _log(f"\nPASS — ComputerAgent(harness_url=…, kind={kind!r}) → real harness round-trip OK.")
        return 0
    except Exception as exc:  # noqa: BLE001
        tail = _tail(log_path)
        if tail:
            print(f"\n─── server log tail ───\n{tail}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return _fail(str(exc) or type(exc).__name__)
    finally:
        teardown(proc)


def _extract_text(messages: list) -> str:
    """Pull assistant text from the streamed SDKMessage dicts (terminal result first)."""
    for m in reversed(messages):
        if isinstance(m, dict):
            if m.get("type") == "result" and isinstance(m.get("result"), str):
                return m["result"]
            msg = m.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                if any(parts):
                    return "".join(parts)
            if isinstance(content, str) and content:
                return content
    return ""


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
