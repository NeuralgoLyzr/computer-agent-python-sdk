"""Where the agent's MCP servers meet everything else that writes options.

`mcp_servers` is a dict two independent sources write into — the identity loader
(a GAP repo's `gap_tools` and any `agent.yaml` HTTP servers) and the agent's own
bindings. The overlay above this merge is a shallow `dict.update`, so getting
this wrong makes a GAP repo's tools vanish the moment an MCP server is bound,
with nothing in the configuration looking wrong.
"""

from __future__ import annotations

from computeragent.harness.coordinator import HarnessCoordinator
from computeragent.mcp.binding import McpBinding
from computeragent.mcp.client import ToolSpec
from computeragent.protocol_types import AgentManifest
from computeragent.substrates import LocalSubstrate

SCHEMA = {"type": "object", "properties": {}}
BINDING = McpBinding(
    server_id="srv_1", action_names=("get_weather",), tool_name="Weather", persist_auth=False
)


class StubClient:
    def __init__(self, tools=None):
        self._tools = tools if tools is not None else [ToolSpec("get_weather", "W", SCHEMA)]

    async def list_tools(self, server_id, *, allowlist=()):
        if not allowlist:
            return list(self._tools)
        return [t for t in self._tools if t.name in set(allowlist)]

    async def execute(self, server_id, tool_name, arguments):
        return {"success": True, "result": "ok"}


class StubEngine:
    name = "claude-agent-sdk"


class StubLoader:
    """Stands in for the identity loader — the other writer of these options."""

    name = "stub"

    def __init__(self, options=None):
        self._options = options or {}

    async def load(self, *, source, target_engine, workspace_path):
        return AgentManifest(name="stub", options=dict(self._options))


_DEFAULT = object()


async def boot(
    tmp_path,
    *,
    loader_options=None,
    overlay=None,
    bindings=(BINDING,),
    engine=None,
    client=_DEFAULT,
):
    coord = HarnessCoordinator(
        engine=engine or StubEngine(),
        substrate=LocalSubstrate(),
        identity_loader=StubLoader(loader_options),
        source=tmp_path,
        envs={},
        options_overlay=overlay,
        mcp_bindings=bindings,
        mcp_client=StubClient() if client is _DEFAULT else client,
    )
    await coord._ensure_booted()
    return coord._manifest.options or {}


# ── attachment ───────────────────────────────────────────────────────────────


async def test_the_bound_server_lands_under_mcp_servers(tmp_path):
    options = await boot(tmp_path)

    assert options["mcp_servers"]["Weather"]["type"] == "sdk"


async def test_nothing_is_attached_when_no_bindings_exist(tmp_path):
    options = await boot(tmp_path, bindings=())

    assert "mcp_servers" not in options


async def test_nothing_is_attached_without_a_client(tmp_path):
    """The client is None exactly when no MCP base was configured, and that case
    is already refused at construction — this is the belt to that braces."""
    options = await boot(tmp_path, client=None)

    assert "mcp_servers" not in options


# ── coexistence with the identity loader ─────────────────────────────────────


async def test_a_gap_repos_own_tools_survive_the_merge(tmp_path):
    """THE regression this file exists for. A shallow update here would delete
    `gap_tools`, and the GAP repo's script tools would simply stop existing."""
    options = await boot(
        tmp_path,
        loader_options={"mcp_servers": {"gap_tools": {"type": "sdk", "name": "gap_tools"}}},
    )

    assert set(options["mcp_servers"]) == {"gap_tools", "Weather"}


async def test_an_agent_yaml_http_server_survives_too(tmp_path):
    options = await boot(
        tmp_path,
        loader_options={
            "mcp_servers": {"docs": {"type": "http", "url": "https://d.invalid", "headers": {}}}
        },
    )

    assert options["mcp_servers"]["docs"]["type"] == "http"
    assert options["mcp_servers"]["Weather"]["type"] == "sdk"


async def test_a_callers_own_mcp_servers_option_survives(tmp_path):
    """It arrives through the shallow overlay, so it is the value most likely to
    be clobbered by a careless merge."""
    options = await boot(
        tmp_path,
        overlay={"mcp_servers": {"mine": {"type": "http", "url": "https://m.invalid"}}},
    )

    assert set(options["mcp_servers"]) == {"mine", "Weather"}


async def test_an_existing_server_of_the_same_name_is_not_overwritten(tmp_path, caplog):
    """Theirs wins: a loader-declared server is something the repo author chose,
    and a binding cannot know it is colliding."""
    theirs = {"type": "http", "url": "https://theirs.invalid"}

    with caplog.at_level("WARNING", logger="computeragent.harness"):
        options = await boot(tmp_path, loader_options={"mcp_servers": {"Weather": theirs}})

    assert options["mcp_servers"]["Weather"] == theirs
    assert any("already configured" in r.getMessage() for r in caplog.records)


# ── allowed_tools: extend, never create ──────────────────────────────────────


async def test_an_existing_allowlist_is_extended(tmp_path):
    options = await boot(tmp_path, loader_options={"allowed_tools": ["Read", "Glob"]})

    assert options["allowed_tools"] == ["Read", "Glob", "mcp__Weather"]


async def test_an_absent_allowlist_stays_absent(tmp_path):
    """Creating a list where none existed flips "every tool allowed" into "only
    these", silently stripping Read/Write/Bash. Measured, not theorised."""
    options = await boot(tmp_path)

    assert "allowed_tools" not in options


async def test_an_empty_allowlist_stays_empty(tmp_path):
    """`[]` is falsy and must be treated as absent, not as a list to extend —
    otherwise the one grant becomes the ONLY permitted tool."""
    options = await boot(tmp_path, loader_options={"allowed_tools": []})

    assert options["allowed_tools"] == []


async def test_the_grant_is_not_duplicated_if_already_present(tmp_path):
    options = await boot(tmp_path, loader_options={"allowed_tools": ["mcp__Weather"]})

    assert options["allowed_tools"] == ["mcp__Weather"]


# ── engines that cannot read the grant ───────────────────────────────────────


async def test_nothing_is_attached_for_a_non_claude_engine(tmp_path, caplog):
    """gitclaw's allowedTools is a strict exact-match set with no server-level
    form, so an `mcp__<server>` entry there matches nothing and filters out
    EVERY tool, built-ins included."""

    class GitAgent(StubEngine):
        name = "gitagent"

    with caplog.at_level("WARNING", logger="computeragent.harness"):
        options = await boot(tmp_path, engine=GitAgent())

    assert "mcp_servers" not in options
    assert any("no MCP support" in r.getMessage() for r in caplog.records)


# ── ordering against the loader's hardening hook ─────────────────────────────


async def test_harden_still_gets_the_last_word(tmp_path):
    """A GAP repo's compliance constraints must outrank everything, including
    anything this merge wrote."""

    class HardeningLoader(StubLoader):
        async def load(self, *, source, target_engine, workspace_path):
            manifest = await StubLoader.load(
                self, source=source, target_engine=target_engine, workspace_path=workspace_path
            )
            object.__setattr__(
                manifest, "harden", lambda opts: {**opts, "permission_mode": "default"}
            )
            return manifest

    coord = HarnessCoordinator(
        engine=StubEngine(),
        substrate=LocalSubstrate(),
        identity_loader=HardeningLoader(),
        source=tmp_path,
        envs={},
        mcp_bindings=(BINDING,),
        mcp_client=StubClient(),
    )
    await coord._ensure_booted()
    options = coord._manifest.options

    assert options["permission_mode"] == "default"
    assert "Weather" in options["mcp_servers"]


# ── discovery failures never fail a boot ─────────────────────────────────────


async def test_a_dead_facade_does_not_prevent_the_agent_from_running(tmp_path):
    class Dead:
        async def list_tools(self, *a, **kw):
            raise ConnectionError("facade down")

    options = await boot(tmp_path, client=Dead(), loader_options={"allowed_tools": ["Read"]})

    assert "mcp_servers" not in options
    # And crucially the allowlist is untouched — a failed attach must not
    # narrow what the agent could already do.
    assert options["allowed_tools"] == ["Read"]
