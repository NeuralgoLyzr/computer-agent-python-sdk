"""Regression: the SRS auto-wire must work with DISCOVERY-ONLY config.

Before the fix, ``_ingest_base()`` read only the legacy ``AGENTOS_INGEST_URL``
env var — a deployment configured the documented way (just
``AGENTOS_DISCOVERY_URL``) silently got NO policy decider, so console-bound
guardrails were never enforced. Caught by the live E2E campaign against the
deployed console.
"""

from __future__ import annotations

import pytest

from computeragent.policy import srs as srs_mod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AGENTOS_INGEST_URL", "AGENTOS_DISCOVERY_URL", "SRS_BASE_URL", "SRS_POLICY_ID"):
        monkeypatch.delenv(var, raising=False)


def test_explicit_ingest_url_still_wins(monkeypatch):
    monkeypatch.setenv("AGENTOS_INGEST_URL", "https://x.example/agentos/api/ingest/events")
    assert srs_mod._ingest_base() == "https://x.example/agentos/api/ingest"


def test_discovery_fallback_resolves_ingest_base(monkeypatch):
    # No AGENTOS_INGEST_URL — the discovery resolver supplies the endpoint.
    monkeypatch.setattr(
        "computeragent.agentos.ingest_url",
        lambda: "https://console.example/agentos/api/ingest/events",
    )
    assert srs_mod._ingest_base() == "https://console.example/agentos/api/ingest"


def test_authorizer_wires_from_discovery_alone(monkeypatch):
    monkeypatch.setattr(
        "computeragent.agentos.ingest_url",
        lambda: "https://console.example/agentos/api/ingest/events",
    )
    assert srs_mod.srs_authorizer_from_env("some-agent") is not None


def test_nothing_configured_stays_inert(monkeypatch):
    monkeypatch.setattr("computeragent.agentos.ingest_url", lambda: None)
    assert srs_mod._ingest_base() is None
    assert srs_mod.srs_authorizer_from_env("some-agent") is None
