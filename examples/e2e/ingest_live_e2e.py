"""Live E2E — AgentOS HTTP ingest: Python AgentOSHttpSink → agentos-server → MongoDB.

This is the proper end-to-end test for the ingest endpoint that replaced the
in-process Mongo sinks. It is self-contained: it boots a REAL `agentos-server`
subprocess pointed at a THROWAWAY Mongo database, pushes telemetry through the
REAL `AgentOSHttpSink` over HTTP, then asserts the server projected the run into
all five collections — and crucially writes the canonical `chat_sessions` row
(so library sessions show up in the dashboard) and does NOT write the dead
`slack_threads`. Verifies via BOTH a direct Mongo read and the server's
dashboard read API. Drops the test DB on exit.

Two modes (default: synthetic):
  --synthetic   Emit a hand-built event stream (deterministic, free, offline
                model) shaped exactly like the harness coordinator's output.
  --real        Run a real 2-turn ComputerAgent chat (claude-agent-sdk) so the
                full stack runs: agent → coordinator → sink → server → Mongo.
                Requires ANTHROPIC_API_KEY.

Credentials are read from the sibling monorepo's .env
(`../ComputerAgent/.env`): MONGO_URL (required), API_AUTH_USER/PASS (for the
read-API checks), ANTHROPIC_API_KEY (--real only).

Run:
    cd computeragent-py
    uv run python examples/e2e/ingest_live_e2e.py            # synthetic
    uv run python examples/e2e/ingest_live_e2e.py --real     # live Claude run
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
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Resolve the sibling TypeScript monorepo (…/lyzr-experiments/ComputerAgent).
HERE = Path(__file__).resolve()
PY_REPO = HERE.parents[2]  # …/computeragent-py
WORKSPACE = PY_REPO.parent  # …/lyzr-experiments
TS_REPO = WORKSPACE / "ComputerAgent"
AGENTOS_DIR = TS_REPO / "packages" / "agentos-server"

# Load the monorepo .env for MONGO_URL / API_AUTH_* / ANTHROPIC_API_KEY.
load_dotenv(TS_REPO / ".env", override=False)
# Keep the parent process quiet — we drive telemetry through an explicit sink.
os.environ.setdefault("COMPUTERAGENT_OTEL", "disabled")


def _log(msg: str) -> None:
    print(msg, flush=True)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _fail(msg: str) -> int:
    print(f"\nFAIL: {msg}", file=sys.stderr, flush=True)
    return 1


# ── server lifecycle ─────────────────────────────────────────────────────────


def boot_server(*, port: int, db: str, token: str, log_path: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "MONGO_DATABASE": db,  # override:false in load-env.ts → this wins
        "AGENTOS_PORT": str(port),
        "AGENTOS_INGEST_TOKEN": token,
        "CA_BASE": "http://127.0.0.1:1",  # no harness — read routes degrade gracefully
        "TRACE_BACKEND": "clickhouse",  # ping fails (fine); server still serves
        "COMPUTERAGENT_OTEL": "disabled",
        "AGENTOS_SEED_DEFAULT": "0",
    }
    logf = open(log_path, "w")  # noqa: SIM115 - closed in teardown
    proc = subprocess.Popen(
        ["pnpm", "exec", "tsx", "src/index.ts"],
        cwd=str(AGENTOS_DIR),
        env=env,
        stdout=logf,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # own process group so we can kill tsx + children
    )
    proc._logf = logf  # type: ignore[attr-defined]
    return proc


def wait_for_health(base: str, *, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base}/agentos/api/health", timeout=3.0)
            if r.status_code == 200 and r.json().get("mongo") == "up":
                return
            last = f"status={r.status_code} body={r.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            last = repr(exc)
        time.sleep(0.5)
    raise TimeoutError(f"server health never reached mongo=up within {timeout}s (last: {last})")


def teardown_server(proc: subprocess.Popen, log_path: Path) -> None:
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


def _tail(path: Path, n: int = 25) -> str:
    with contextlib.suppress(Exception):
        return "\n".join(path.read_text().splitlines()[-n:])
    return ""


# ── workloads ────────────────────────────────────────────────────────────────


async def run_synthetic(*, url: str, token: str, agent_name: str, probe: str, marker: str) -> str:
    """Emit a coordinator-shaped event stream through the real sink. Returns the
    session_id."""
    from computeragent.telemetry.event import TelemetryEvent
    from computeragent.telemetry.sinks import AgentOSHttpSink

    session_id = "ca_synthetic_" + uuid.uuid4().hex[:12]
    prompt = f"Reply with exactly {marker} (probe={probe})."
    common = dict(
        session_id=session_id,
        agent_name=agent_name,
        agent_description="ingest live e2e",
    )
    events = [
        TelemetryEvent(
            kind="session_started",
            **common,
            payload={
                "harness_mode": True,  # → source.type == "library"
                "prompt": prompt,
                "model": "anthropic:claude-sonnet-4-5",
                "system_prompt": "Reply in one short sentence.",
                "allowed_tools": ["Read"],
                "permission_mode": "bypassPermissions",
                "max_turns": 2,
            },
        ),
        TelemetryEvent(
            kind="tool_use",
            **common,
            payload={"tool_use_id": "t1", "tool_name": "Read", "tool_input": {"path": "x"}},
        ),
        TelemetryEvent(
            kind="tool_result",
            **common,
            payload={"tool_use_id": "t1", "is_error": False, "content": "ok"},
        ),
        TelemetryEvent(
            kind="assistant_message",
            **common,
            payload={"text": marker, "model": "claude-sonnet-4-5", "block_count": 1},
        ),
        TelemetryEvent(
            kind="usage_snapshot",
            **common,
            payload={
                "model": "claude-sonnet-4-5",
                "total_cost_usd": 0.012,
                "usage": {"input_tokens": 120, "output_tokens": 34},
            },
        ),
        TelemetryEvent(
            kind="session_ended",
            **common,
            payload={
                "is_error": False,
                "subtype": "success",
                "result": marker,
                "num_turns": 2,
                "total_cost_usd": 0.012,
                "duration_ms": 1500,
                "usage": {"input_tokens": 120, "output_tokens": 34},
            },
        ),
    ]
    sink = AgentOSHttpSink(url=url, token=token)
    for ev in events:
        await sink.emit(ev)
    await sink.flush()
    await sink.aclose()
    return session_id


async def run_real(*, url: str, token: str, agent_name: str, probe: str, marker: str) -> str:
    """Run a real 2-turn ComputerAgent chat whose telemetry flows through the
    sink. Returns the session_id."""
    import tempfile

    from computeragent import ComputerAgent
    from computeragent.telemetry import Pipeline
    from computeragent.telemetry.sinks import AgentOSHttpSink

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("--real needs ANTHROPIC_API_KEY (none in env / .env)")

    pipeline = Pipeline(sinks=[AgentOSHttpSink(url=url, token=token)])
    workdir = Path(tempfile.mkdtemp(prefix="ingest-e2e-"))

    async with ComputerAgent(
        source=workdir,
        engine="claude-agent-sdk",
        runtime="local",
        agent_name=agent_name,
        agent_description="ingest live e2e",
        options={
            "model": "claude-sonnet-4-5",
            "system_prompt": "Reply in one short sentence.",
            "permission_mode": "bypassPermissions",
            "max_turns": 2,
        },
        telemetry_pipeline=pipeline,
    ) as agent:
        r1 = await agent.chat(
            f"Without using any tool, reply with exactly the text {marker} "
            f"and nothing else. (probe={probe})"
        )
        await agent.chat("Without using any tool, reply with the single word OK.")

    await pipeline.flush()
    await pipeline.aclose()
    return r1.session_id


# ── verification ─────────────────────────────────────────────────────────────


def verify_mongo(*, mongo_url: str, db: str, agent_name: str, session_id: str, marker: str, probe: str) -> None:
    from pymongo import MongoClient

    client: MongoClient = MongoClient(mongo_url, serverSelectionTimeoutMS=8000)
    try:
        d = client[db]
        reg = list(d["agent_registry"].find({"_id": agent_name}))
        logs = list(d["agent_logs"].find({"bot": agent_name}))
        sess = d["sessions"].find_one({"_id": session_id})
        chat = d["chat_sessions"].find_one({"_id": session_id})
        msgs = list(d["agent_messages"].find({"sessionId": session_id}))
        slack = list(d["slack_threads"].find({}))

        _log("\n─── direct Mongo verification ───")
        _log(f"  agent_registry({agent_name}) : {len(reg)}")
        _log(f"  agent_logs(bot={agent_name})  : {len(logs)}")
        _log(f"  sessions({session_id})        : {'1' if sess else '0'}")
        _log(f"  chat_sessions({session_id})   : {'1' if chat else '0'}")
        _log(f"  agent_messages(session)       : {len(msgs)}")
        _log(f"  slack_threads(total)          : {len(slack)}")

        # agent_registry — library source (harness_mode), one doc.
        assert len(reg) == 1, f"expected 1 agent_registry doc, got {len(reg)}"
        assert reg[0].get("harness") == "claude-agent-sdk"
        assert (reg[0].get("source") or {}).get("type") == "library", (
            f"expected library source, got {reg[0].get('source')}"
        )

        # agent_logs — the rollup, keyed on bot + agentName, ok=True.
        assert len(logs) >= 1, "no agent_logs row written"
        log = logs[-1]
        assert log.get("agentName") == agent_name, "agent_logs missing parallel agentName field"
        assert log.get("ok") is True, f"agent_logs ok != True: {log.get('ok')}"
        assert log["_id"].startswith("log_"), f"unexpected log _id: {log['_id']}"

        # sessions — transcript with the seeded prompt + assistant answer.
        assert sess is not None, "no sessions doc written"
        assert sess.get("agentName") == agent_name
        entries = sess.get("entries") or []
        users = [e for e in entries if e.get("type") == "user"]
        assistants = [e for e in entries if e.get("type") == "assistant"]
        assert any(probe in (e.get("text") or "") for e in users), (
            f"no user entry carrying the prompt probe; entries={entries}"
        )
        assert any(marker in (e.get("text") or "") for e in assistants), (
            f"no assistant entry carrying the marker; entries={entries}"
        )
        assert sess.get("ok") is True and sess.get("endedAt") is not None, "session not closed"

        # chat_sessions — THE fix: the row the dashboard list reads.
        assert chat is not None, "chat_sessions row missing — session won't show in the dashboard list"
        assert chat.get("agent") == agent_name, f"chat_sessions.agent != {agent_name}"

        # agent_messages — per-event archive, no rollup kinds.
        assert len(msgs) >= 2, f"expected >=2 agent_messages, got {len(msgs)}"
        kinds = {m.get("kind") for m in msgs}
        assert "assistant_message" in kinds, f"agent_messages missing assistant_message; kinds={kinds}"
        assert "session_started" not in kinds and "session_ended" not in kinds, (
            f"rollup kinds leaked into agent_messages: {kinds}"
        )
        assert all(m["_id"].startswith("msg_") for m in msgs)

        # slack_threads — must be dead (we dropped it).
        assert len(slack) == 0, f"slack_threads should not be written, found {len(slack)}"
    finally:
        client.close()


def verify_read_api(*, base: str, agent_name: str, session_id: str, marker: str) -> None:
    user = os.environ.get("API_AUTH_USER")
    pw = os.environ.get("API_AUTH_PASS")
    if not user or not pw:
        _log("\n(skip dashboard read-API checks — API_AUTH_USER/PASS not set)")
        return
    auth = (user, pw)
    _log("\n─── dashboard read-API verification ───")
    with httpx.Client(base_url=base, auth=auth, timeout=10.0) as c:
        agents = c.get("/agentos/api/agents").raise_for_status().json()["agents"]
        mine = next((a for a in agents if a["name"] == agent_name), None)
        assert mine is not None, f"agent {agent_name} not in GET /agents"
        _log(f"  GET /agents → sessionCount={mine['sessionCount']} logCount={mine['logCount']} liveChatCapable={mine['liveChatCapable']}")
        assert mine["sessionCount"] >= 1, "sessionCount should be >= 1 (chat_sessions drives this)"
        assert mine["logCount"] >= 1, "logCount should be >= 1"
        assert mine["liveChatCapable"] is False, "library agent should not be liveChatCapable"

        sessions = c.get("/agentos/api/sessions", params={"bot": agent_name}).raise_for_status().json()["sessions"]
        ids = [s["sessionId"] for s in sessions]
        _log(f"  GET /sessions?bot={agent_name} → {ids}")
        assert session_id in ids, f"session {session_id} not in GET /sessions list"

        detail = c.get(f"/agentos/api/sessions/{session_id}").raise_for_status().json()
        texts = " ".join(e.get("text", "") for e in detail.get("entries", []))
        _log(f"  GET /sessions/{session_id} → {len(detail.get('entries', []))} entries, bot={detail.get('bot')}")
        assert detail.get("bot") == agent_name
        assert marker in texts, f"marker not in transcript entries: {texts[:200]!r}"


# ── main ─────────────────────────────────────────────────────────────────────


async def main() -> int:
    mode = "synthetic"
    if "--real" in sys.argv:
        mode = "real"
    elif "--synthetic" in sys.argv:
        mode = "synthetic"

    mongo_url = os.environ.get("MONGO_URL") or os.environ.get("AGENTOS_MONGO_URL")
    if not mongo_url:
        return _fail("MONGO_URL not set (expected in ../ComputerAgent/.env)")
    if not AGENTOS_DIR.exists():
        return _fail(f"agentos-server not found at {AGENTOS_DIR}")

    run_id = uuid.uuid4().hex[:8]
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    url = f"{base}/agentos/api/ingest/events"
    token = f"live-test-{run_id}"
    db = f"ca_ingest_livetest_{run_id}"
    agent_name = f"ingest-live-{run_id}"
    probe = f"probe-{run_id}"
    marker = "INGEST_LIVE_OK"
    log_path = Path(f"/tmp/agentos-server-{run_id}.log")

    _log(f"→ mode          : {mode}")
    _log(f"→ agentos-server: {AGENTOS_DIR}")
    _log(f"→ ingest URL    : {url}")
    _log(f"→ test DB       : {db}  (dropped on exit)")
    _log(f"→ agent_name    : {agent_name}")
    _log(f"→ server log    : {log_path}")
    _log("─" * 64)

    proc = boot_server(port=port, db=db, token=token, log_path=log_path)
    try:
        _log("→ booting agentos-server …")
        wait_for_health(base)
        _log("→ server healthy (mongo up)")

        _log(f"→ driving workload ({mode}) …")
        if mode == "real":
            session_id = await run_real(url=url, token=token, agent_name=agent_name, probe=probe, marker=marker)
        else:
            session_id = await run_synthetic(url=url, token=token, agent_name=agent_name, probe=probe, marker=marker)
        _log(f"→ session_id    : {session_id}")

        # Give the server a beat to finish the last writes.
        await asyncio.sleep(0.5)

        verify_mongo(mongo_url=mongo_url, db=db, agent_name=agent_name, session_id=session_id, marker=marker, probe=probe)
        verify_read_api(base=base, agent_name=agent_name, session_id=session_id, marker=marker)

        _log("\nPASS — Python sink → agentos-server ingest → MongoDB projection verified end-to-end.")
        return 0
    except Exception as exc:  # noqa: BLE001
        tail = _tail(log_path)
        if tail:
            print(f"\n─── agentos-server log tail ───\n{tail}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return _fail(str(exc) or type(exc).__name__)
    finally:
        teardown_server(proc, log_path)
        # Drop the throwaway DB so nothing leaks into the cluster.
        try:
            from pymongo import MongoClient

            cl: MongoClient = MongoClient(mongo_url, serverSelectionTimeoutMS=8000)
            cl.drop_database(db)
            cl.close()
            _log(f"→ dropped test DB {db}")
        except Exception as exc:  # noqa: BLE001
            print(f"(warning: failed to drop test DB {db}: {exc})", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
