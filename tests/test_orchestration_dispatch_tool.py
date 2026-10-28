"""Surface A: the in-session ``dispatch_many`` tool, its injection into the
engine, and the policy bypass that keeps it reachable."""

from __future__ import annotations

from pathlib import Path

import pytest

from computeragent.harness.coordinator import HarnessCoordinator
from computeragent.orchestration.contract import (
    ORCHESTRATION_INTERNAL_TOOLS,
    ORCHESTRATION_SUPERVISED_DISPATCH_MANY_TOOL_NAME,
    DispatchResultItem,
    OrchestrationSpec,
    is_orchestration_internal_tool,
)
from computeragent.orchestration.dispatch_tool import (
    DISPATCH_SERVER_NAME,
    build_dispatch_server,
)
from computeragent.protocol_types import AgentManifest
from computeragent.substrates import LocalSubstrate
from fakes.scripted_engine import ScriptedEngine

TOOL = ORCHESTRATION_SUPERVISED_DISPATCH_MANY_TOOL_NAME


# ── the contract ───────────────────────────────────────────────────────────


def test_the_tool_name_is_the_fully_namespaced_form_the_model_sees():
    # That is the string the policy gate is handed; a different spelling here
    # would be silent — the tool would just read as ordinary agent capability.
    assert TOOL == "mcp__orchestration__dispatch_many"
    assert TOOL in ORCHESTRATION_INTERNAL_TOOLS


def test_only_the_listed_tools_are_control_plane():
    assert is_orchestration_internal_tool(TOOL) is True
    assert is_orchestration_internal_tool("Bash") is False
    assert is_orchestration_internal_tool("mcp__jira__create_issue") is False


# ── the tool itself ────────────────────────────────────────────────────────


async def test_the_server_is_named_orchestration():
    server = build_dispatch_server(lambda _items: None)
    assert server["name"] == DISPATCH_SERVER_NAME
    assert server["type"] == "sdk"


async def test_the_handler_forwards_the_workers_and_returns_their_reports():
    seen: list = []

    async def dispatch(items):
        seen.extend(items)
        return [DispatchResultItem(id="w1", status="completed", text="all done")]

    from computeragent.orchestration.dispatch_tool import _to_request_item

    item = _to_request_item(
        {"task": "do it", "id": "w1", "role": "builder", "scope": {"write": ["src/**"]}}
    )
    results = await dispatch([item])
    assert seen[0].task == "do it"
    assert seen[0].scope.write == ("src/**",)
    assert results[0].text == "all done"


def test_a_worker_without_a_task_is_not_a_request_item():
    from computeragent.orchestration.dispatch_tool import _to_request_item

    assert _to_request_item({"id": "w1"}) is None
    assert _to_request_item({"task": ""}) is None
    assert _to_request_item("not a dict") is None


def test_request_item_fields_are_filtered_by_type():
    from computeragent.orchestration.dispatch_tool import _to_request_item

    item = _to_request_item(
        {"task": "t", "id": 7, "tools": ["Read", 3], "scope": {"read": ["docs/**"]}}
    )
    assert item.id is None
    assert item.tools == ("Read",)
    assert item.scope.write is None
    assert item.scope.read == ("docs/**",)


# ── injection into the engine ──────────────────────────────────────────────


class Loader:
    name = "test"

    def __init__(self, spec, options=None):
        self.spec = spec
        self.options = options or {}

    async def load(self, *, source, target_engine, workspace_path):
        return AgentManifest(
            name="o", version="1", options=dict(self.options), orchestration=self.spec
        )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("x\n")
    return repo


def build(workspace: Path, engine, spec, options=None) -> HarnessCoordinator:
    return HarnessCoordinator(
        engine=engine,
        substrate=LocalSubstrate(),
        identity_loader=Loader(spec, options),
        source=workspace,
        envs={},
        agent_name="o",
    )


async def test_a_supervised_orchestrator_is_handed_a_dispatch_hook(workspace: Path):
    engine = ScriptedEngine(["ok"])
    coordinator = build(workspace, engine, OrchestrationSpec(mode="supervised"))
    _state, stream = await coordinator.run_chat("go")
    [ev async for ev in stream]
    assert engine.contexts[0].dispatch is not None
    await coordinator.dispose()


async def test_a_roster_orchestrator_is_not(workspace: Path):
    engine = ScriptedEngine(["ok"])
    coordinator = build(workspace, engine, OrchestrationSpec(mode="roster"))
    _state, stream = await coordinator.run_chat("go")
    [ev async for ev in stream]
    assert engine.contexts[0].dispatch is None
    await coordinator.dispose()


async def test_the_hook_runs_a_real_wave_and_returns_reports(workspace: Path):
    engine = ScriptedEngine(["ok"])
    coordinator = build(
        workspace, engine, OrchestrationSpec(mode="supervised", isolation="worktree")
    )
    _state, stream = await coordinator.run_chat("go")
    events = [ev async for ev in stream]

    from computeragent.orchestration.contract import DispatchRequestItem

    hook = engine.contexts[0].dispatch
    reports = await hook([DispatchRequestItem(task="do the thing", id="w1")])
    assert [r.id for r in reports] == ["w1"]
    assert reports[0].status == "completed"
    assert events  # the orchestrator's own turn still streamed normally
    await coordinator.dispose()


# ── the engine's own wiring ────────────────────────────────────────────────


def build_options(*, dispatch=None, allowed=None, decider=None):
    import asyncio

    from computeragent.engines.claude_agent import _build_options
    from computeragent.protocol_types import EngineContext

    async def _no_permission(_req):
        return {}

    options = {}
    if allowed is not None:
        options["allowed_tools"] = allowed
    ctx = EngineContext(
        session_id="ca_1",
        manifest=AgentManifest(name="a", version="1", options=options),
        workspace_path=Path("/tmp"),
        envs={},
        user_message_queue=[],
        on_permission_request=_no_permission,
        cancel_event=asyncio.Event(),
        policy_decider=decider,
        dispatch=dispatch,
    )
    return _build_options(ctx)


async def test_the_engine_injects_the_server_only_when_a_hook_is_present():
    async def hook(_items):
        return []

    assert DISPATCH_SERVER_NAME not in (build_options().mcp_servers or {})
    assert DISPATCH_SERVER_NAME in build_options(dispatch=hook).mcp_servers


async def test_an_existing_allowlist_gains_the_dispatch_tool():
    # Otherwise injecting the server hands the orchestrator a tool its own
    # allowlist then forbids, and the fan-out is unreachable for a reason
    # nothing reports.
    async def hook(_items):
        return []

    options = build_options(dispatch=hook, allowed=["Read"])
    assert options.allowed_tools == ["Read", TOOL]


async def test_no_allowlist_is_created_where_none_existed():
    async def hook(_items):
        return []

    assert not build_options(dispatch=hook).allowed_tools


async def test_the_policy_gate_allows_the_control_plane_tool_without_consulting_it():
    # A default-deny policy written for the agent's real tools would deny this
    # by omission, and a decider that cannot reach its service would fail it
    # closed — both killing the orchestrator's turn.
    consulted: list[str] = []

    async def decider(tool_name, tool_input, context):
        from claude_agent_sdk.types import PermissionResultDeny

        consulted.append(tool_name)
        return PermissionResultDeny(message="denied", interrupt=False)

    options = build_options(decider=decider)
    verdict = await options.can_use_tool(TOOL, {"workers": []}, None)
    assert getattr(verdict, "behavior", None) == "allow"
    assert consulted == []


async def test_every_other_tool_still_reaches_the_decider():
    consulted: list[str] = []

    async def decider(tool_name, tool_input, context):
        from claude_agent_sdk.types import PermissionResultDeny

        consulted.append(tool_name)
        return PermissionResultDeny(message="denied", interrupt=False)

    options = build_options(decider=decider)
    verdict = await options.can_use_tool("Bash", {}, None)
    assert getattr(verdict, "behavior", None) == "deny"
    assert consulted == ["Bash"]
