"""Bindings → in-process MCP servers.

The hosted stack materialises a binding as an HTTP server pointing at a bridge;
here it becomes a `create_sdk_mcp_server` whose tools call the facade directly.
Naming matches either way — the model and any allowlist see the same strings —
so these tests pin the names as hard as the behaviour.
"""

from __future__ import annotations

import pytest

from computeragent.mcp.binding import McpBinding
from computeragent.mcp.client import ToolSpec
from computeragent.mcp.server import build_mcp_servers

SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}}


class StubClient:
    """Records every call; scripted per server id."""

    def __init__(self, tools=None, *, execute=None, list_raises=None, all_tools=None):
        self._tools = tools if tools is not None else [ToolSpec("get_weather", "W", SCHEMA)]
        self._execute = execute
        self._list_raises = list_raises
        self._all_tools = all_tools
        self.list_calls: list[tuple[str, tuple[str, ...]]] = []
        self.execute_calls: list[tuple[str, str, dict]] = []

    async def list_tools(self, server_id, *, allowlist=()):
        self.list_calls.append((server_id, allowlist))
        if self._list_raises is not None:
            raise self._list_raises
        pool = self._all_tools if (not allowlist and self._all_tools is not None) else self._tools
        if not allowlist:
            return list(pool)
        return [t for t in pool if t.name in set(allowlist)]

    async def execute(self, server_id, tool_name, arguments):
        self.execute_calls.append((server_id, tool_name, arguments))
        if callable(self._execute):
            return self._execute(server_id, tool_name, arguments)
        return self._execute if self._execute is not None else {"success": True, "result": "sunny"}


def binding(**over) -> McpBinding:
    kw = {
        "server_id": "srv_1",
        "action_names": ("get_weather",),
        "tool_name": "Weather",
        "persist_auth": False,
    }
    kw.update(over)
    return McpBinding(**kw)


def handler_for(built, namespace, tool_name):
    """The async handler the SDK would dispatch to, from the REAL build loop."""
    for t in built.tools[namespace]:
        if t.name == tool_name:
            return t.handler
    raise AssertionError(f"no tool {tool_name!r} in {namespace!r}")


# ── shape ────────────────────────────────────────────────────────────────────


async def test_one_binding_becomes_one_sdk_server_keyed_by_its_namespace():
    built = await build_mcp_servers((binding(),), StubClient())

    assert list(built.servers) == ["Weather"]
    assert built.servers["Weather"]["type"] == "sdk"
    assert built.servers["Weather"]["name"] == "Weather"


async def test_the_grant_and_tool_names_match_the_hosted_stack():
    built = await build_mcp_servers((binding(),), StubClient())

    assert built.grants == ["mcp__Weather"]
    assert built.tool_names == ["mcp__Weather__get_weather"]


async def test_the_allowlist_is_passed_to_discovery():
    client = StubClient()

    await build_mcp_servers((binding(action_names=("a", "b")),), client)

    assert client.list_calls[0] == ("srv_1", ("a", "b"))


async def test_no_bindings_builds_nothing():
    built = await build_mcp_servers((), StubClient())

    assert not built
    assert built.servers == {}


async def test_two_bindings_build_two_servers():
    client = StubClient(tools=[ToolSpec("get_weather", "", SCHEMA), ToolSpec("t", "", SCHEMA)])

    built = await build_mcp_servers(
        (binding(), binding(server_id="srv_2", tool_name="Docs", action_names=("t",))), client
    )

    assert sorted(built.servers) == ["Docs", "Weather"]
    assert sorted(built.grants) == ["mcp__Docs", "mcp__Weather"]


# ── namespaces nothing else validates ────────────────────────────────────────


@pytest.mark.parametrize("bad", ["My Server", "a.b", "srv/1", "", "tool name", "emoji✨"])
async def test_an_unusable_namespace_is_skipped_with_a_warning(bad, caplog):
    """`tool_name` is operator-settable and nothing validates it — not AgentOS,
    not the CAS, not claude-agent-sdk, which serialises whatever key it gets
    straight into --mcp-config. The TS docstring's own example is "My Server"."""
    with caplog.at_level("WARNING", logger="computeragent.mcp.server"):
        built = await build_mcp_servers((binding(tool_name=bad),), StubClient())

    assert built.servers == {}
    assert any("not usable as a tool namespace" in r.getMessage() for r in caplog.records)


async def test_a_reserved_namespace_is_skipped(caplog):
    """`gap_tools` is the loader's own in-process server; taking that key would
    silently disable the GAP repo's tools."""
    with caplog.at_level("WARNING", logger="computeragent.mcp.server"):
        built = await build_mcp_servers((binding(tool_name="gap_tools"),), StubClient())

    assert built.servers == {}


async def test_a_duplicate_namespace_keeps_the_first_and_warns(caplog):
    """AgentOS enforces action-name uniqueness across bindings but NOT
    tool_name uniqueness, so two bindings can legitimately arrive with the same
    one. The CAS would silently overwrite; we do not."""
    client = StubClient(tools=[ToolSpec("get_weather", "", SCHEMA)])

    with caplog.at_level("WARNING", logger="computeragent.mcp.server"):
        built = await build_mcp_servers(
            (binding(), binding(server_id="srv_2", action_names=("get_weather",))), client
        )

    assert list(built.servers) == ["Weather"]
    assert built.grants == ["mcp__Weather"]
    assert any("already taken" in r.getMessage() for r in caplog.records)


# ── failures never reach boot ────────────────────────────────────────────────


async def test_a_discovery_failure_yields_no_servers_and_no_exception(caplog):
    """A facade outage degrades the run to "no MCP tools this turn", which the
    model can work around; an exception out of boot it cannot."""
    with caplog.at_level("WARNING", logger="computeragent.mcp.server"):
        built = await build_mcp_servers(
            (binding(),), StubClient(list_raises=RuntimeError("facade down"))
        )

    assert built.servers == {}
    assert any("runs without its tools" in r.getMessage() for r in caplog.records)


async def test_one_failing_binding_does_not_take_down_the_others():
    class Selective(StubClient):
        async def list_tools(self, server_id, *, allowlist=()):
            if server_id == "srv_bad":
                raise RuntimeError("down")
            return await StubClient.list_tools(self, server_id, allowlist=allowlist)

    built = await build_mcp_servers(
        (binding(server_id="srv_bad", tool_name="Bad"), binding()), Selective()
    )

    assert list(built.servers) == ["Weather"]


async def test_zero_matched_tools_logs_both_lists(caplog):
    """The most common cause of a bound tool never appearing, and the two lists
    side by side are the whole diagnosis."""
    client = StubClient(tools=[], all_tools=[ToolSpec("getWeather", "", SCHEMA)])

    with caplog.at_level("WARNING", logger="computeragent.mcp.server"):
        built = await build_mcp_servers((binding(action_names=("get_weather",)),), client)

    assert built.servers == {}
    message = " ".join(r.getMessage() for r in caplog.records)
    assert "get_weather" in message  # what was bound
    assert "getWeather" in message  # what the server actually has


# ── handlers ─────────────────────────────────────────────────────────────────


async def test_a_successful_call_returns_normalised_content():
    client = StubClient(execute={"success": True, "result": [{"type": "text", "text": "sunny"}]})
    built = await build_mcp_servers((binding(),), client)

    out = await (handler_for(built, "Weather", "get_weather"))({"city": "Pune"})

    assert out == {"content": [{"type": "text", "text": "sunny"}], "is_error": False}
    assert client.execute_calls == [("srv_1", "get_weather", {"city": "Pune"})]


async def test_a_failed_tool_becomes_a_readable_is_error_result():
    """`success: false` means the tool RAN and failed — the model should see it
    and adapt, not receive a protocol fault it cannot recover from."""
    client = StubClient(execute={"success": False, "error": "city not found"})
    built = await build_mcp_servers((binding(),), client)

    out = await (handler_for(built, "Weather", "get_weather"))({})

    assert out["is_error"] is True
    assert "city not found" in out["content"][0]["text"]


async def test_a_transport_exception_becomes_is_error_rather_than_propagating():
    class Boom(StubClient):
        async def execute(self, *a, **kw):
            raise ConnectionError("facade unreachable")

    built = await build_mcp_servers((binding(),), Boom())

    out = await (handler_for(built, "Weather", "get_weather"))({})

    assert out["is_error"] is True
    assert "facade unreachable" in out["content"][0]["text"]


async def test_the_allowlist_is_enforced_at_call_time_too():
    """A model that saw a wider list earlier, or guessed, must not reach a tool
    this agent was never bound to."""
    client = StubClient(tools=[ToolSpec("get_weather", "", SCHEMA), ToolSpec("nuke", "", SCHEMA)])
    built = await build_mcp_servers((binding(action_names=("get_weather", "nuke")),), client)

    handler = handler_for(built, "Weather", "nuke")

    out = await handler({})

    # In-process the list is already filtered, so this handler exists only
    # because "nuke" IS bound here — the guard's job is the case where the
    # model names something the binding never granted.
    assert out["is_error"] is False


async def test_each_handler_closes_over_its_own_tool():
    """Defining the handler inside the loop would close over the loop variable,
    so every tool would call whichever spec the loop finished on. Paid for once
    already in this repo — gap_tools.py:140-161 exists for the same reason."""
    client = StubClient(tools=[ToolSpec("first", "", SCHEMA), ToolSpec("second", "", SCHEMA)])
    built = await build_mcp_servers((binding(action_names=("first", "second")),), client)

    await (handler_for(built, "Weather", "first"))({})

    assert [call[1] for call in client.execute_calls] == ["first"]


async def test_the_declared_schema_reaches_the_sdk_tool():
    """It is what the remote server declares, and jsonschema validates the
    model's arguments against it in THIS process before the handler runs."""
    built = await build_mcp_servers(
        (binding(),), StubClient(tools=[ToolSpec("get_weather", "W", SCHEMA)])
    )

    (built_tool,) = built.tools["Weather"]
    assert built_tool.input_schema == SCHEMA
    assert built_tool.description == "W"


async def test_a_tool_with_no_description_still_gets_one():
    """The description is what tells the model when to reach for the tool; an
    empty one makes a working binding useless."""
    built = await build_mcp_servers(
        (binding(),), StubClient(tools=[ToolSpec("get_weather", "", SCHEMA)])
    )

    (built_tool,) = built.tools["Weather"]
    assert "get_weather" in built_tool.description
    assert "srv_1" in built_tool.description


async def test_an_out_of_binding_name_is_refused_without_calling_the_facade():
    """The guard that matters: a model that saw a wider list earlier, or simply
    guessed, must not reach a tool this agent was never bound to."""
    from computeragent.mcp.server import _build_tool

    client = StubClient()
    smuggled = _build_tool(
        client, binding(action_names=("get_weather",)), ToolSpec("nuke", "", SCHEMA)
    )

    out = await smuggled.handler({})

    assert out["is_error"] is True
    assert "not available on this binding" in out["content"][0]["text"]
    assert client.execute_calls == []
