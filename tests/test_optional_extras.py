"""Optional-extra behavior: missing deps raise a clean ImportError with hints."""

from __future__ import annotations

import importlib
import sys

import pytest


def _strip_modules(prefixes: list[str]) -> dict[str, object]:
    """Pop matching modules from sys.modules and return them for restoration."""
    removed: dict[str, object] = {}
    for name in list(sys.modules):
        if any(name == p or name.startswith(p + ".") for p in prefixes):
            removed[name] = sys.modules.pop(name)
    return removed


def _restore(modules: dict[str, object]) -> None:
    for name, mod in modules.items():
        sys.modules[name] = mod  # type: ignore[assignment]


def test_otelsink_import_error_when_otel_missing(monkeypatch):
    # Force opentelemetry imports to fail.
    removed = _strip_modules(["opentelemetry", "computeragent.telemetry.sinks.otel"])
    try:
        # Block re-import of any opentelemetry submodule by installing a
        # MetaPathFinder that raises ImportError for the prefix.
        class _Blocker:
            def find_spec(self, name, path=None, target=None):
                if name == "opentelemetry" or name.startswith("opentelemetry."):
                    raise ImportError(f"blocked {name} for test")
                return

        finder = _Blocker()
        sys.meta_path.insert(0, finder)
        try:
            with pytest.raises(ImportError) as exc:
                importlib.import_module("computeragent.telemetry.sinks.otel")
            assert "computer-agent-py[otel]" in str(exc.value)
        finally:
            sys.meta_path.remove(finder)
    finally:
        _restore(removed)


def test_agentos_sink_import_error_when_motor_missing(monkeypatch):
    removed = _strip_modules(["motor", "computeragent.telemetry.sinks.agentos"])
    try:

        class _Blocker:
            def find_spec(self, name, path=None, target=None):
                if name == "motor" or name.startswith("motor."):
                    raise ImportError(f"blocked {name} for test")
                return

        finder = _Blocker()
        sys.meta_path.insert(0, finder)
        try:
            with pytest.raises(ImportError) as exc:
                importlib.import_module("computeragent.telemetry.sinks.agentos")
            assert "computer-agent-py[agentos]" in str(exc.value)
        finally:
            sys.meta_path.remove(finder)
    finally:
        _restore(removed)


def test_lazy_attribute_access_on_sinks_package_works():
    """``from computeragent.telemetry.sinks import OtelSink`` works when the
    extra is installed (dev env has it). Verify the lazy ``__getattr__``."""
    from computeragent.telemetry import sinks

    assert sinks.OtelSink is not None
    assert sinks.AgentRegistrySink is not None
    with pytest.raises(AttributeError):
        sinks.Nonexistent  # noqa: B018
