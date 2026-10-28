"""Worker fan-out primitive: an orchestrator spawns worker sessions as SIBLINGS
in the same process, runs them, and collects a result for each.

This is the Python generalisation of the TS ``spawn-child.ts`` — each worker is
its own session (own workdir/telemetry/lifecycle), parent-linked for console
nesting, bounded by a concurrency limit. It reuses the exact existing
per-session machinery (:class:`~computeragent.harness.coordinator.HarnessCoordinator`);
nothing here changes how a top-level session runs.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from .composite_decider import compose_deciders
from .message_text import extract_message_text
from .scope_guard import AllowAllDecider, ScopeGuardDecider
from .worker_mcp import inherit_mcp_into, resolve_worker_mcp

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from ..protocol_types import EngineDriver, IdentitySource
    from ..telemetry.pipeline import Pipeline
    from .contract import OrchestrationSpec, SessionMode, WorkerMcpPolicy, WorkerScope
    from .wave_events import WaveReporter

logger = logging.getLogger("computeragent.orchestration.spawn")

WorkerStatus = Literal["completed", "errored"]


# ── what a fan-out needs to know about its orchestrator ────────────────────


@dataclass(frozen=True)
class OrchestratorRef:
    """The slice of a running orchestrator that a fan-out reads.

    The TS port takes ``ServerDeps`` + a ``Session`` off a registry; this SDK
    has neither, because one ``ComputerAgent`` is one session. So the harness
    hands the fan-out this snapshot instead — every field is something the
    parent coordinator already holds.
    """

    session_id: str
    engine: EngineDriver
    engine_name: str
    engine_options: Mapping[str, Any]
    envs: Mapping[str, str]
    workdir: Path
    agent_id: str | None = None
    agent_name: str | None = None
    policy_decider: Any | None = None
    telemetry_pipeline: Pipeline | None = None
    session_store: Any | None = None
    project_key: str = "default"
    orchestration: OrchestrationSpec | None = None
    git_token: str | None = None


# ── worker inputs and outputs ──────────────────────────────────────────────


@dataclass(frozen=True)
class WorkerSpec:
    """One worker to spawn. Higher layers (the GAP crew, the roster) translate
    their own entry shape into this."""

    #: Stable id within the roster, e.g. ``w1``. Carried onto the result.
    id: str
    #: The (referenced) GAP source the worker runs.
    source: IdentitySource
    #: The task prompt handed to the worker as its user message.
    task: str
    #: Engine name (defaults to the orchestrator's).
    engine: str | None = None
    #: Per-worker engine option overrides merged over the loader's options —
    #: model, allowed_tools, max_turns, max_budget_usd, hooks, etc.
    options: dict[str, Any] | None = None
    #: Extra envs merged over the orchestrator's.
    envs: Mapping[str, str] | None = None
    #: Telemetry identity mode. Defaults to ``"separate"``.
    session_mode: SessionMode = "separate"
    #: Filesystem scope — ``scope.write`` becomes the worker's scope-guard.
    scope: WorkerScope | None = None
    #: Which of the orchestrator's MCP servers this worker inherits. ``None`` ⇒
    #: ``"inherit"`` (all of them) — the roster runner resolves the crew entry's
    #: own ``mcp`` against the orchestration block before it gets here.
    mcp: WorkerMcpPolicy | None = None
    #: An explicit manifest for an INLINE worker, bypassing the identity loader.
    #: See :func:`inline_worker_manifest`.
    inline_manifest: Any | None = None


@dataclass(frozen=True)
class WorkerUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True)
class SessionOutcome:
    """The collectable outcome of a run — shared by workers and the
    orchestrator."""

    session_id: str
    status: WorkerStatus
    #: Best-effort assistant text: the final result, else concatenated text.
    text: str
    usage: WorkerUsage
    error: str | None = None


@dataclass(frozen=True)
class WorkerResult:
    """The outcome of one worker session.

    :func:`spawn_child_session` never raises — a failure is reported here
    (fail-soft), so one worker can't lose the wave.
    """

    id: str
    session_id: str
    status: WorkerStatus
    text: str
    usage: WorkerUsage
    error: str | None = None
    #: Files this worker wrote (shared-codebase mode; git diff attribution).
    artifacts: tuple[str, ...] = field(default_factory=tuple)


def _errored(worker_id: str, session_id: str, exc: BaseException) -> WorkerResult:
    return WorkerResult(
        id=worker_id,
        session_id=session_id,
        status="errored",
        text="",
        usage=WorkerUsage(),
        error=str(exc) or type(exc).__name__,
    )


# ── the inline identity ────────────────────────────────────────────────────


class _InlineManifestLoader:
    """Hands back a fixed manifest instead of reading the workspace.

    DIVERGENCE FROM THE TS PORT, and the reason for it. TS materialises a
    synthetic two-line ``agent.yaml`` INTO the worker's worktree, which
    overwrites the orchestrator repo's own manifest — then has to restore it
    from the baseline before the worker runs, because otherwise every worker's
    branch modifies ``agent.yaml`` with different content and any wave with ≥2
    workers conflicts on a file none of them touched.

    This SDK does not need the write at all: an identity loader here is just an
    object that returns a manifest, so the worker gets its minimal identity
    without a byte landing in the worktree. Same end state, one whole class of
    conflict removed. The restore is still performed by the dispatcher as a
    belt-and-braces measure, so the invariant ("a worker's diff carries only the
    worker's own work") survives a future loader that does write.
    """

    name = "inline"

    def __init__(self, manifest: Any) -> None:
        self._manifest = manifest

    async def load(self, *, source: Any, target_engine: str, workspace_path: Path) -> Any:
        del source, target_engine, workspace_path
        return self._manifest


def inline_worker_manifest(worker_id: str) -> Any:
    """A minimal synthetic identity for an inline worker.

    Deliberately carries no system prompt: the dispatcher supplies the worker's
    role and scope through its options overlay, exactly as the TS worker-options
    builder does.
    """
    from ..protocol_types import AgentManifest

    return AgentManifest(name=worker_id, version="0", options={})


# ── running one worker ─────────────────────────────────────────────────────


def _build_child_coordinator(
    parent: OrchestratorRef,
    spec: WorkerSpec,
    *,
    child_session_id: str,
    options: dict[str, Any],
    workdir: Path | None,
) -> Any:
    from ..engines import resolve_engine
    from ..harness.coordinator import HarnessCoordinator
    from ..substrates import LocalSubstrate

    named = spec.engine
    engine = (
        parent.engine if named is None or named == parent.engine_name else resolve_engine(named)
    )
    engine_name = engine.name

    if spec.inline_manifest is not None:
        loader: Any = _InlineManifestLoader(spec.inline_manifest)
    else:
        from ..harness import _AutoIdentityLoader

        loader = _AutoIdentityLoader()

    # The scope-guard runs FIRST and the inherited guardrail second: the local
    # check is free and an inherited decider may be a network round-trip, so an
    # out-of-scope write is denied without paying for one.
    members: list[Any] = []
    if spec.scope is not None and spec.scope.write:
        members.append(
            ScopeGuardDecider(
                scope_write=tuple(spec.scope.write),
                workspace=workdir or parent.workdir,
            )
        )
        logger.info(
            "scope_guard.attached: worker=%s scope_write=%s", spec.id, list(spec.scope.write)
        )
    # A bound guardrail must not be escapable by fanning out: without this the
    # orchestrator's every tool call is gated while its workers' Bash/Write/MCP
    # calls run through nothing.
    if parent.policy_decider is not None:
        members.append(parent.policy_decider)
        logger.info(
            "policy.inherited_by_worker: worker=%s parent_session=%s",
            spec.id,
            parent.session_id,
        )
    if not members:
        # Nobody is subscribed to a worker's stream, so a permission request it
        # raises can never be answered. An engine that always routes tool calls
        # through the permission path would hang on its first one and take the
        # whole wave with it. An allow-all decider short-circuits that; the
        # worker's writes are still bounded by its own worktree.
        members.append(AllowAllDecider())

    return HarnessCoordinator(
        engine=engine,
        substrate=LocalSubstrate(),
        identity_loader=loader,
        source=(workdir if workdir is not None else spec.source),
        envs={**parent.envs, **(spec.envs or {})},
        session_store=parent.session_store,
        # `sharedId` ⇒ stamp the orchestrator's telemetry id so the worker folds
        # into the one conversation; `separate` ⇒ its own id (a nested child).
        session_id=(parent.session_id if spec.session_mode == "sharedId" else child_session_id),
        agent_name=spec.id,
        agent_id=parent.agent_id,
        policy_decider=compose_deciders(tuple(members)),
        telemetry_pipeline=parent.telemetry_pipeline,
        project_key=parent.project_key,
        options_overlay=options,
        git_token=parent.git_token,
        parent_session_id=parent.session_id,
        parent_agent_id=parent.agent_id,
        # A worker never fans out. Its own repo may well declare an
        # `orchestration:` block — a crew repo reused as a worker is the obvious
        # case — and without this each level would clone, open worktrees and
        # spend the caller's budget again, with nothing bounding the depth.
        drive_orchestration=False,
    ), engine_name


async def collect_session(coordinator: Any, task: str) -> SessionOutcome:
    """Run one turn on ``coordinator`` and drain it into text + usage + status.

    Reused for both worker results and the orchestrator's roster output.
    """
    result_text = ""
    assistant_text: list[str] = []
    error: str | None = None
    input_tokens = 0
    output_tokens = 0
    cost_usd: float | None = None

    def _on_text(t: str) -> None:
        assistant_text.append(t)

    def _on_result(r: str) -> None:
        nonlocal result_text
        result_text = r

    state, stream = await coordinator.run_chat(task)
    try:
        async for ev in stream:
            if ev.kind == "usage_snapshot":
                input_tokens += ev.input_tokens or 0
                output_tokens += ev.output_tokens or 0
                if ev.cost_usd is not None:
                    # delta ⇒ SUM; cumulative/absent ⇒ last value wins.
                    cost_usd = (
                        (cost_usd or 0.0) + ev.cost_usd
                        if ev.cost_semantic == "delta"
                        else ev.cost_usd
                    )
            elif ev.kind == "sdk_message":
                extract_message_text(ev.payload, _on_text, _on_result)
    except Exception as exc:  # noqa: BLE001 — a worker failure is a result, not a raise
        error = str(exc) or type(exc).__name__

    return SessionOutcome(
        session_id=state.session_id,
        status="errored" if error else "completed",
        text=result_text or "".join(assistant_text),
        usage=WorkerUsage(
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd
        ),
        error=error,
    )


async def spawn_child_session(
    parent: OrchestratorRef,
    spec: WorkerSpec,
    reporter: WaveReporter | None = None,
    *,
    workdir: Path | None = None,
) -> WorkerResult:
    """Spawn ONE worker as a sibling session and run it to completion.

    Never raises: a create/engine failure comes back as a :class:`WorkerResult`
    with ``status="errored"``.
    """
    child_session_id = f"sess_{uuid.uuid4().hex[:12]}"

    # Tools follow the same rule as envs: a worker is part of the orchestrator's
    # run, so it gets the orchestrator's MCP servers unless its crew entry
    # opted out.
    engine_name = spec.engine or parent.engine_name
    base_options = dict(spec.options or {})
    options = inherit_mcp_into(
        base_options,
        parent.engine_options,
        resolve_worker_mcp(
            spec.mcp, parent.orchestration.worker_mcp if parent.orchestration else None
        ),
        engine_name,
    )
    if options is not base_options and not _is_inline(spec):
        # A referenced worker is a DIFFERENT repo running with the caller's MCP
        # credential. That is the configured default, but it should never be
        # invisible — this is the line to grep when auditing who got what.
        logger.warning(
            "worker_mcp.inherited_by_referenced_worker: worker=%s parent_session=%s "
            "source=%r servers=%s",
            spec.id,
            parent.session_id,
            spec.source,
            sorted(options.get("mcp_servers") or {}),
        )

    coordinator: Any = None
    try:
        coordinator, engine_name = _build_child_coordinator(
            parent,
            spec,
            child_session_id=child_session_id,
            options=options,
            workdir=workdir,
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft; one worker must not lose the wave
        return _errored(spec.id, child_session_id, exc)

    # Announce the worker only once its session actually exists: the session id
    # is the caller's join key, and a construction failure has no transcript to
    # join to (it reports as a completion with an error instead).
    if reporter is not None:
        reporter.worker_started(
            worker_id=spec.id,
            worker_session_id=child_session_id,
            task=spec.task or None,
            engine=engine_name,
            model=options.get("model") if isinstance(options.get("model"), str) else None,
            scope=_scope_payload(spec.scope),
        )

    try:
        outcome = await collect_session(coordinator, spec.task)
    except Exception as exc:  # noqa: BLE001 — guard anyway so the wave survives
        return _errored(spec.id, child_session_id, exc)
    finally:
        with_suppressed_dispose = getattr(coordinator, "dispose", None)
        if callable(with_suppressed_dispose):
            try:
                await coordinator.dispose()
            except Exception:  # noqa: BLE001 — teardown must not mask the result
                logger.debug("worker %s: dispose failed", spec.id, exc_info=True)

    return WorkerResult(
        id=spec.id,
        session_id=outcome.session_id,
        status=outcome.status,
        text=outcome.text,
        usage=outcome.usage,
        error=outcome.error,
    )


def _is_inline(spec: WorkerSpec) -> bool:
    return spec.inline_manifest is not None


def _scope_payload(scope: WorkerScope | None) -> dict[str, list[str]] | None:
    if scope is None:
        return None
    out: dict[str, list[str]] = {}
    if scope.write:
        out["write"] = list(scope.write)
    if scope.read:
        out["read"] = list(scope.read)
    return out or None


async def dispatch_workers(
    parent: OrchestratorRef,
    specs: Sequence[WorkerSpec],
    *,
    max_parallel: int = 4,
    reporter: WaveReporter | None = None,
) -> list[WorkerResult]:
    """Fan out N workers as sibling sessions, bounded by ``max_parallel``.

    Concurrency is CODE-controlled (a fixed pool), not model-controlled. Never
    raises; each slot's failure is captured in its :class:`WorkerResult`.
    """
    if not specs:
        return []
    limit = max(1, min(max_parallel, len(specs)))
    results: list[WorkerResult | None] = [None] * len(specs)
    cursor = 0
    cursor_lock = asyncio.Lock()

    async def run_one() -> None:
        nonlocal cursor
        while True:
            async with cursor_lock:
                index = cursor
                cursor += 1
            if index >= len(specs):
                return
            spec = specs[index]
            started_at = time.monotonic()
            result = await spawn_child_session(parent, spec, reporter)
            results[index] = result
            # Reported from HERE, not from inside `spawn_child_session`: every
            # outcome — created-then-ran, construction failure, engine failure —
            # funnels through this one await, so no return path can silently
            # skip its completion.
            if reporter is not None:
                reporter.worker_completed(
                    worker_id=result.id,
                    worker_session_id=result.session_id,
                    status=result.status,
                    error=result.error,
                    text=result.text or None,
                    artifacts=list(result.artifacts) or None,
                    usage=_usage_payload(result.usage),
                    duration_ms=(time.monotonic() - started_at) * 1000.0,
                )

    await asyncio.gather(*(run_one() for _ in range(limit)))
    return [r for r in results if r is not None]


def _usage_payload(usage: WorkerUsage) -> dict[str, Any]:
    out: dict[str, Any] = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }
    if usage.cost_usd is not None:
        out["cost_usd"] = usage.cost_usd
    return out


__all__ = [
    "OrchestratorRef",
    "SessionOutcome",
    "WorkerResult",
    "WorkerSpec",
    "WorkerStatus",
    "WorkerUsage",
    "collect_session",
    "dispatch_workers",
    "inline_worker_manifest",
    "spawn_child_session",
]
