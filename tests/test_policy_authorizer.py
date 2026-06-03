"""``PolicyToolAuthorizer`` — the ``can_use_tool`` adapter."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from computeragent.policy import (
    PolicyAction,
    PolicyDecision,
    PolicyEngine,
    PolicyInput,
    PolicyPrincipal,
    PolicyResource,
    PolicyResult,
    PolicyToolAuthorizer,
)


class StubEngine:
    """Test engine that returns canned decisions and records calls."""

    name = "stub"

    def __init__(
        self, decision: PolicyDecision, reason: str = "", raises: BaseException | None = None
    ):
        self._decision = decision
        self._reason = reason
        self._raises = raises
        self.calls: list[PolicyInput] = []

    async def evaluate(self, input: PolicyInput) -> PolicyResult:
        self.calls.append(input)
        if self._raises is not None:
            raise self._raises
        return PolicyResult(decision=self._decision, reason=self._reason, engine=self.name)

    async def aclose(self) -> None:
        pass


def _ctx(session_id: str = "sess_x") -> Any:
    # ToolPermissionContext is a dataclass in claude-agent-sdk. Build a
    # minimal MagicMock that exposes the fields the authorizer reads.
    mock = MagicMock(spec=ToolPermissionContext)
    mock.session_id = session_id
    return mock


@pytest.mark.asyncio
async def test_allow_returns_permission_result_allow():
    engine = StubEngine(PolicyDecision.ALLOW)
    auth = PolicyToolAuthorizer(
        engine=engine,
        principal_resolver=lambda c: PolicyPrincipal(id="alice", groups=["engineer"]),
    )
    result = await auth("Read", {"path": "/x"}, _ctx())
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {"path": "/x"}


@pytest.mark.asyncio
async def test_deny_returns_permission_result_deny_with_reason():
    engine = StubEngine(PolicyDecision.DENY, reason="tool not on allowlist")
    auth = PolicyToolAuthorizer(
        engine=engine,
        principal_resolver=lambda c: PolicyPrincipal(id="alice"),
    )
    result = await auth("Bash", {"command": "ls"}, _ctx())
    assert isinstance(result, PermissionResultDeny)
    assert "allowlist" in result.message


@pytest.mark.asyncio
async def test_principal_resolver_receives_context():
    seen: list[Any] = []

    def resolver(ctx):
        seen.append(ctx)
        return PolicyPrincipal(id="alice")

    engine = StubEngine(PolicyDecision.ALLOW)
    auth = PolicyToolAuthorizer(engine=engine, principal_resolver=resolver)
    ctx = _ctx("sess_42")
    await auth("Read", {}, ctx)
    assert seen == [ctx]


@pytest.mark.asyncio
async def test_resource_resolver_overrides_default():
    seen: list[Any] = []

    def resource_resolver(ctx):
        seen.append(ctx)
        return PolicyResource(agent_name="my-agent", model="m")

    engine = StubEngine(PolicyDecision.ALLOW)
    auth = PolicyToolAuthorizer(
        engine=engine,
        principal_resolver=lambda c: PolicyPrincipal(id="alice"),
        resource_resolver=resource_resolver,
    )
    await auth("Read", {}, _ctx())
    assert seen and engine.calls[-1].resource.agent_name == "my-agent"


@pytest.mark.asyncio
async def test_context_resolver_overrides_default():
    engine = StubEngine(PolicyDecision.ALLOW)
    auth = PolicyToolAuthorizer(
        engine=engine,
        principal_resolver=lambda c: PolicyPrincipal(id="alice"),
        context_resolver=lambda c: {"env": "prod", "tenant": "lyzr"},
    )
    await auth("Read", {}, _ctx())
    ctx_dict = engine.calls[-1].context
    assert ctx_dict["env"] == "prod"
    assert ctx_dict["tenant"] == "lyzr"


@pytest.mark.asyncio
async def test_async_principal_resolver_supported():
    async def resolver(ctx):
        return PolicyPrincipal(id="alice", groups=["engineer"])

    engine = StubEngine(PolicyDecision.ALLOW)
    auth = PolicyToolAuthorizer(engine=engine, principal_resolver=resolver)
    result = await auth("Read", {}, _ctx())
    assert isinstance(result, PermissionResultAllow)
    assert engine.calls[-1].principal.groups == ["engineer"]


@pytest.mark.asyncio
async def test_action_carries_tool_name_and_input():
    engine = StubEngine(PolicyDecision.ALLOW)
    auth = PolicyToolAuthorizer(
        engine=engine,
        principal_resolver=lambda c: PolicyPrincipal(id="alice"),
    )
    await auth("Read", {"path": "/etc/passwd"}, _ctx())
    a = engine.calls[-1].action
    assert isinstance(a, PolicyAction)
    assert a.tool_name == "Read"
    assert a.tool_input == {"path": "/etc/passwd"}


@pytest.mark.asyncio
async def test_engine_raises_returns_deny():
    engine = StubEngine(PolicyDecision.ALLOW, raises=RuntimeError("boom"))
    auth = PolicyToolAuthorizer(
        engine=engine,
        principal_resolver=lambda c: PolicyPrincipal(id="alice"),
    )
    result = await auth("Read", {}, _ctx())
    assert isinstance(result, PermissionResultDeny)
    assert "boom" in result.message


@pytest.mark.asyncio
async def test_on_deny_interrupt_flag_propagated():
    engine = StubEngine(PolicyDecision.DENY, reason="no")
    auth = PolicyToolAuthorizer(
        engine=engine,
        principal_resolver=lambda c: PolicyPrincipal(id="alice"),
        on_deny="interrupt",
    )
    result = await auth("Bash", {}, _ctx())
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is True


def test_engine_protocol_is_satisfied_by_real_engines():
    """The Protocol matches the engines structurally — useful for mypy."""
    from computeragent.policy import OpaPolicyEngine
    from computeragent.policy.cedar import CedarPolicyEngine

    opa = OpaPolicyEngine(url="http://nope", policy_path="p")
    cedar = CedarPolicyEngine(policies="")
    # Structural Protocol — both must duck-type as PolicyEngine.
    eng: PolicyEngine = opa  # noqa: F841 - mypy/type-checker assertion
    eng = cedar  # noqa: F841
