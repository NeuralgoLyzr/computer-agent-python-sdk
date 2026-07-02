"""Every public symbol from ``claude_agent_sdk`` must resolve in ``computeragent``."""

from __future__ import annotations

import claude_agent_sdk

import computeragent


def test_every_upstream_all_symbol_is_re_exported():
    upstream = set(getattr(claude_agent_sdk, "__all__", []))
    missing = [n for n in upstream if not hasattr(computeragent, n)]
    assert not missing, f"missing re-exports: {missing}"


def test_types_module_is_complete():
    """``computeragent.types`` mirrors ``claude_agent_sdk.types``."""
    import claude_agent_sdk.types as upstream_types

    import computeragent.types as our_types

    upstream_names = {n for n in dir(upstream_types) if not n.startswith("_")}
    ours = {n for n in dir(our_types) if not n.startswith("_")}
    missing = upstream_names - ours
    assert not missing, f"computeragent.types missing: {missing}"


def test_query_and_client_are_proxied():
    """The two entry points must be our wrapped versions, not the upstream ones."""
    assert computeragent.query is not claude_agent_sdk.query
    assert computeragent.ClaudeSDKClient is not claude_agent_sdk.ClaudeSDKClient


def test_telemetry_addons_present():
    assert hasattr(computeragent, "configure")
    assert hasattr(computeragent, "PiiRedactor")
    assert hasattr(computeragent, "GuardrailFilter")
    assert hasattr(computeragent, "TelemetryEvent")
    assert hasattr(computeragent, "Pipeline")


def test_version_is_ours_not_upstream():
    from importlib.metadata import version

    # Our __version__ tracks the packaged version, not upstream's.
    assert computeragent.__version__ == version("computer-agent-py")
    assert computeragent.upstream_version == claude_agent_sdk.__version__
