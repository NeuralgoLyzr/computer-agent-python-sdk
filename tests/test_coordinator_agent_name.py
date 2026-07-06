"""Telemetry agent-name resolution (``HarnessCoordinator._resolve_agent_name``).

Precedence: explicit ``agent_name`` → stable ``agent_id`` → manifest ``name``.
The manifest name is last because the GAP/passthrough loaders fall back to the
cloned repo *directory* name when ``agent.yaml`` has no ``name`` — which would
surface the git repo in the obs UI instead of the agent. Preferring ``agent_id``
means a stable-id'd agent shows its id, never the repo.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from computeragent.harness.coordinator import HarnessCoordinator


def _coordinator(*, agent_name, agent_id, manifest_name) -> HarnessCoordinator:
    # Build a bare coordinator without booting substrate/engine — we only
    # exercise the pure name-resolution method.
    c = object.__new__(HarnessCoordinator)
    c._agent_name = agent_name
    c._agent_id = agent_id
    c._manifest = SimpleNamespace(name=manifest_name) if manifest_name else None
    c._warned_manifest_name_fallback = False
    return c


def test_explicit_agent_name_wins():
    c = _coordinator(agent_name="Nordassist QA", agent_id="abc123", manifest_name="repo-dir")
    assert c._resolve_agent_name() == "Nordassist QA"


def test_agent_id_preferred_over_manifest_dir_name():
    # No explicit name → the stable id wins over the repo-dir manifest name.
    c = _coordinator(agent_name=None, agent_id="abc123", manifest_name="repo-dir")
    assert c._resolve_agent_name() == "abc123"


def test_falls_back_to_manifest_name_and_warns(caplog):
    c = _coordinator(agent_name=None, agent_id=None, manifest_name="repo-dir")
    with caplog.at_level(logging.WARNING, logger="computeragent.harness"):
        assert c._resolve_agent_name() == "repo-dir"
    assert any("falling back to manifest name" in r.message for r in caplog.records)


def test_fallback_warning_fires_at_most_once():
    c = _coordinator(agent_name=None, agent_id=None, manifest_name="repo-dir")
    c._resolve_agent_name()
    assert c._warned_manifest_name_fallback is True
    # Second call must not re-warn (flag already latched).
    c._resolve_agent_name()
    assert c._warned_manifest_name_fallback is True
