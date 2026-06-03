"""``ClaudeSDKClient`` wrapper lifecycle + control passthrough."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from claude_agent_sdk.types import ResultMessage

from computeragent import ClaudeSDKClient, configure


def _result(text="done", cost=0.01, turns=1):
    r = MagicMock(spec=ResultMessage)
    r.result = text
    r.total_cost_usd = cost
    r.num_turns = turns
    r.is_error = False
    r.subtype = "success"
    r.session_id = "sess_xyz"
    r.usage = {"input_tokens": 10, "output_tokens": 5}
    r.duration_api_ms = 100.0
    r.model = "claude-sonnet-4-6"
    return r


@pytest.mark.asyncio
async def test_context_manager_connect_disconnect(fake_upstream_client, stub_sink):
    configure(sinks=[stub_sink])
    async with ClaudeSDKClient(options=None) as client:
        # Inner upstream client was instantiated and connected.
        [inner] = fake_upstream_client.instances
        assert inner.connected is True
        assert client is not None
    # Exit triggered disconnect on the inner.
    assert inner.connected is False
    kinds = [e.kind for e in stub_sink.events]
    assert kinds[0] == "session_started"
    assert kinds[-1] == "session_ended"


@pytest.mark.asyncio
async def test_query_forwards_to_inner(fake_upstream_client, stub_sink):
    configure(sinks=[stub_sink])
    async with ClaudeSDKClient(options=None) as client:
        await client.query("a question", session_id="my-sess")
    [inner] = fake_upstream_client.instances
    assert inner.queried == [("a question", "my-sess")]


@pytest.mark.asyncio
async def test_receive_messages_taps_telemetry(fake_upstream_client, stub_sink):
    r = _result(text="ok")
    fake_upstream_client.messages = [r]
    configure(sinks=[stub_sink])
    async with ClaudeSDKClient(options=None) as client:
        msgs = [m async for m in client.receive_messages()]
    assert msgs == [r]
    # usage_snapshot emitted from the ResultMessage; session_ended on exit.
    kinds = [e.kind for e in stub_sink.events]
    assert "usage_snapshot" in kinds
    assert kinds[-1] == "session_ended"


@pytest.mark.asyncio
async def test_interrupt_passes_through(fake_upstream_client, stub_sink):
    configure(sinks=[stub_sink])
    async with ClaudeSDKClient(options=None) as client:
        await client.interrupt()
    [inner] = fake_upstream_client.instances
    assert inner.interrupts == 1


@pytest.mark.asyncio
async def test_control_methods_pass_through(fake_upstream_client, stub_sink):
    configure(sinks=[stub_sink])
    async with ClaudeSDKClient(options=None) as client:
        await client.set_model("new-model")
        await client.set_permission_mode("bypassPermissions")
        await client.get_context_usage()
        await client.get_mcp_status()
        assert await client.get_server_info() == {"name": "fake"}
        await client.reconnect_mcp_server("srv")
        await client.toggle_mcp_server("srv", True)
        await client.stop_task("task1")
        await client.rewind_files("msg1")
    [inner] = fake_upstream_client.instances
    assert inner.set_models == ["new-model"]
    inner.set_permission_mode.assert_called_once_with("bypassPermissions")
    inner.get_context_usage.assert_called_once()
    inner.get_mcp_status.assert_called_once()
    inner.reconnect_mcp_server.assert_called_once_with("srv")
    inner.toggle_mcp_server.assert_called_once_with("srv", True)
    inner.stop_task.assert_called_once_with("task1")
    inner.rewind_files.assert_called_once_with("msg1")
