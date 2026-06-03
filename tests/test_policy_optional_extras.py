"""``CedarPolicyEngine`` raises a clean ``ImportError`` when ``cedarpy`` is missing."""

from __future__ import annotations

import importlib
import sys

import pytest


def _strip_modules(prefixes: list[str]) -> dict[str, object]:
    removed: dict[str, object] = {}
    for name in list(sys.modules):
        if any(name == p or name.startswith(p + ".") for p in prefixes):
            removed[name] = sys.modules.pop(name)
    return removed


def _restore(modules: dict[str, object]) -> None:
    for name, mod in modules.items():
        sys.modules[name] = mod  # type: ignore[assignment]


def test_cedar_import_error_when_cedarpy_missing():
    removed = _strip_modules(["cedarpy", "computeragent.policy.cedar"])
    try:

        class _Blocker:
            def find_spec(self, name, path=None, target=None):
                if name == "cedarpy" or name.startswith("cedarpy."):
                    raise ImportError(f"blocked {name} for test")
                return

        finder = _Blocker()
        sys.meta_path.insert(0, finder)
        try:
            with pytest.raises(ImportError) as exc:
                importlib.import_module("computeragent.policy.cedar")
            assert "computer-agent-py[cedar]" in str(exc.value)
        finally:
            sys.meta_path.remove(finder)
    finally:
        _restore(removed)


def test_lazy_attribute_access_on_policy_package_works():
    """``from computeragent.policy import CedarPolicyEngine`` works when extra installed."""
    from computeragent import policy

    assert policy.OpaPolicyEngine is not None
    assert policy.CedarPolicyEngine is not None
    with pytest.raises(AttributeError):
        policy.NonexistentEngine  # noqa: B018


def test_opa_works_without_optional_extras():
    """OPA only needs httpx which is core — verify it's importable with no extras."""
    from computeragent.policy import OpaPolicyEngine

    engine = OpaPolicyEngine(url="http://nope:8181", policy_path="p")
    assert engine.name == "opa"
