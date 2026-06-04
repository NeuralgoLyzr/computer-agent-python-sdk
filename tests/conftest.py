"""Shared test fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture(autouse=True)
def _reset_pipeline(monkeypatch):
    """Drop the cached telemetry pipeline before/after each test, and disable
    env-driven auto-attachment of OtelSink / AgentOSHttpSink. Tests that
    exercise those sinks attach them explicitly via ``configure(sinks=[...])``.
    """
    monkeypatch.setenv("COMPUTERAGENT_OTEL", "disabled")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("AGENTOS_INGEST_URL", raising=False)
    monkeypatch.delenv("AGENTOS_INGEST_TOKEN", raising=False)

    from computeragent.telemetry import config as _cfg

    _cfg._reset_for_tests()
    yield
    _cfg._reset_for_tests()


class StubSink:
    """Telemetry sink that records every event it receives."""

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.flushed = 0
        self.closed = 0

    async def emit(self, event: Any) -> None:
        self.events.append(event)

    async def flush(self, timeout: float = 5.0) -> None:
        self.flushed += 1

    async def aclose(self) -> None:
        self.closed += 1


@pytest.fixture
def stub_sink() -> StubSink:
    return StubSink()


def make_upstream_stream(messages: list[Any]):
    """Build an async generator that yields the given message sequence.

    Used to monkeypatch ``claude_agent_sdk.query``.
    """

    async def _gen(**_kwargs: Any) -> AsyncIterator[Any]:
        for msg in messages:
            yield msg

    return _gen


@pytest.fixture
def upstream_query_factory(monkeypatch):
    """Patch ``claude_agent_sdk.query`` with a generator over the supplied messages.

    Usage::

        def test_thing(upstream_query_factory):
            upstream_query_factory([assistant_msg, result_msg])
            # now `claude_agent_sdk.query()` yields exactly those.
    """
    import claude_agent_sdk

    def _factory(messages: list[Any]) -> None:
        monkeypatch.setattr(claude_agent_sdk, "query", make_upstream_stream(messages))

    return _factory


@pytest.fixture
def fake_upstream_client(monkeypatch):
    """Replace ``claude_agent_sdk.ClaudeSDKClient`` with a stand-in.

    The fake records calls and supports the lifecycle methods needed by our
    proxy. Tests can set the message sequence returned by ``receive_messages``.
    """
    import claude_agent_sdk

    class _FakeClient:
        instances: list[_FakeClient] = []
        messages: list[Any] = []

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.connected = False
            self.queried: list[tuple[Any, str]] = []
            self.interrupts = 0
            self.set_models: list[str | None] = []
            _FakeClient.instances.append(self)
            self.connect = AsyncMock(side_effect=self._connect)
            self.disconnect = AsyncMock(side_effect=self._disconnect)
            self.set_permission_mode = AsyncMock()
            self.get_context_usage = AsyncMock(return_value={"used": 0})
            self.get_mcp_status = AsyncMock(return_value={"servers": []})
            self.reconnect_mcp_server = AsyncMock()
            self.toggle_mcp_server = AsyncMock()
            self.stop_task = AsyncMock()
            self.rewind_files = AsyncMock()

        async def _connect(self, *_args: Any) -> None:
            self.connected = True

        async def _disconnect(self) -> None:
            self.connected = False

        async def query(self, prompt: Any, session_id: str = "default") -> None:
            self.queried.append((prompt, session_id))

        async def interrupt(self) -> None:
            self.interrupts += 1

        async def set_model(self, model: str | None = None) -> None:
            self.set_models.append(model)

        async def get_server_info(self) -> dict[str, Any] | None:
            return {"name": "fake"}

        async def receive_messages(self):
            for m in _FakeClient.messages:
                yield m

        async def receive_response(self):
            for m in _FakeClient.messages:
                yield m

    _FakeClient.instances = []
    _FakeClient.messages = []
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _FakeClient)
    return _FakeClient
