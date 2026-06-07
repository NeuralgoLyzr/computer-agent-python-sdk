"""AgentOS discovery resolver — one config (AGENTOS_DISCOVERY_URL) + cak_ key,
absolute URLs read from the server, no endpoint path baked into the SDK."""

from __future__ import annotations

import httpx
import pytest
import respx

from computeragent import agentos

DISCOVERY = "https://agentos.test/agentos/api/discovery"

DOC = {
    "version": "1",
    "endpoints": {
        "ingest": "https://agentos.test/agentos/api/ingest/events",
        "api": "https://agentos.test/agentos/api/v1",
        "otel": "https://agentos.test/agentos/api/otel",
        "messages": "https://agentos.test/agentos/api/v1/messages",
    },
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    # Wipe every AgentOS connectivity var so each test starts from nothing, and
    # clear the process-wide discovery memo.
    for var in (
        "AGENTOS_DISCOVERY_URL",
        "AGENTOS_INGEST_URL",
        "AGENTOS_API_URL",
        "AGENTOS_OTEL_URL",
        "COMPUTERAGENT_HARNESS_TOKEN",
        "AGENTOS_INGEST_TOKEN",
        "COMPUTERAGENT_HARNESS_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    agentos._reset_cache_for_tests()
    yield
    agentos._reset_cache_for_tests()


@respx.mock
def test_discovery_resolves_all_endpoints(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTOS_DISCOVERY_URL", DISCOVERY)
    route = respx.get(DISCOVERY).mock(return_value=httpx.Response(200, json=DOC))

    assert agentos.ingest_url() == "https://agentos.test/agentos/api/ingest/events"
    assert agentos.api_url() == "https://agentos.test/agentos/api/v1"
    assert agentos.otel_url() == "https://agentos.test/agentos/api/otel"
    # Memoized — three resolver calls, one network fetch.
    assert route.call_count == 1


@respx.mock
def test_discovery_sends_cak_bearer(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTOS_DISCOVERY_URL", DISCOVERY)
    monkeypatch.setenv("COMPUTERAGENT_HARNESS_TOKEN", "cak_abc")
    route = respx.get(DISCOVERY).mock(return_value=httpx.Response(200, json=DOC))

    agentos.ingest_url()
    assert route.calls.last.request.headers["authorization"] == "Bearer cak_abc"


def test_explicit_override_wins_without_fetch(monkeypatch: pytest.MonkeyPatch):
    # An explicit full-URL override short-circuits discovery entirely (no network).
    monkeypatch.setenv("AGENTOS_DISCOVERY_URL", DISCOVERY)
    monkeypatch.setenv("AGENTOS_INGEST_URL", "https://override.test/ingest/")
    with respx.mock:
        route = respx.get(DISCOVERY).mock(return_value=httpx.Response(200, json=DOC))
        # ingest comes from the override (trailing slash trimmed); api still discovers.
        assert agentos.ingest_url() == "https://override.test/ingest"
        assert agentos.api_url() == "https://agentos.test/agentos/api/v1"
        assert route.call_count == 1  # only api triggered the fetch


def test_unconfigured_returns_none():
    # No discovery URL, no overrides → every endpoint is None (nothing baked in).
    assert agentos.ingest_url() is None
    assert agentos.api_url() is None
    assert agentos.otel_url() is None


@respx.mock
def test_discovery_failure_is_soft(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTOS_DISCOVERY_URL", DISCOVERY)
    respx.get(DISCOVERY).mock(return_value=httpx.Response(503))
    # A failed fetch yields None (no baked fallback) and is not cached → retried.
    assert agentos.ingest_url() is None


def test_cak_and_harness_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COMPUTERAGENT_HARNESS_TOKEN", "cak_xyz")
    monkeypatch.setenv("COMPUTERAGENT_HARNESS_URL", "https://harness.test/")
    assert agentos.cak() == "cak_xyz"
    assert agentos.harness_url() == "https://harness.test"
    # Legacy ingest-token fallback for the key.
    monkeypatch.delenv("COMPUTERAGENT_HARNESS_TOKEN")
    monkeypatch.setenv("AGENTOS_INGEST_TOKEN", "cak_legacy")
    assert agentos.cak() == "cak_legacy"
