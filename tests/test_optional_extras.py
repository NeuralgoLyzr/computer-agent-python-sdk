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


def test_lazy_attribute_access_on_sinks_package_works():
    """``from computeragent.telemetry.sinks import OtelSink`` works when the
    extra is installed (dev env has it). Verify the lazy ``__getattr__``."""
    from computeragent.telemetry import sinks

    assert sinks.OtelSink is not None
    assert not hasattr(sinks, "AgentOSHttpSink")
    with pytest.raises(AttributeError):
        sinks.Nonexistent  # noqa: B018


def test_importing_the_package_does_not_pull_in_pymongo():
    """The mongo driver is reached for only when a mongo store is built."""
    removed = _strip_modules(["computeragent", "pymongo"])
    try:
        importlib.import_module("computeragent")

        assert "pymongo" not in sys.modules
    finally:
        _strip_modules(["computeragent"])
        _restore(removed)


def test_eagerly_imported_modules_do_not_bind_httpx():
    """httpx must stay a lazy, in-function import on the eager path.

    It cannot be kept out of the process — `claude_agent_sdk` depends on `mcp`,
    which imports httpx eagerly, so it is always present in a base install. What
    IS ours to keep is that no module reached by `import computeragent` binds it
    at import time, which is what `policy/__init__` promises when it says its
    engines are lazy. `policy.srs` is the one that breaks that promise if left
    unattended, because unlike `opa`/`cedar` it is imported eagerly there.
    """
    import computeragent.policy as policy
    import computeragent.policy.srs as srs

    assert not hasattr(srs, "httpx")
    # opa/cedar are reached only through `policy.__getattr__`, so a module-level
    # import there costs the eager path nothing.
    assert "OpaPolicyEngine" not in vars(policy)


def test_mongo_store_import_error_names_its_extra():
    from computeragent.session_stores import resolve_session_store

    removed = _strip_modules(["pymongo"])
    try:

        class _Blocker:
            def find_spec(self, name, path=None, target=None):
                if name == "pymongo" or name.startswith("pymongo."):
                    raise ImportError(f"blocked {name} for test")
                return

        finder = _Blocker()
        sys.meta_path.insert(0, finder)
        try:
            with pytest.raises(ImportError) as exc:
                resolve_session_store({"kind": "mongo", "options": {"url": "mongodb://x"}})
            assert "computer-agent-py[mongo]" in str(exc.value)
        finally:
            sys.meta_path.remove(finder)
    finally:
        _restore(removed)


def test_every_registered_store_kind_is_resolvable_or_names_what_it_needs():
    """A kind in the registry must either build or fail with a message that
    says what to supply — never a bare KeyError or TypeError."""
    from computeragent.session_stores import _REGISTRY, resolve_session_store

    for kind in _REGISTRY:
        try:
            resolve_session_store(kind)
        except (ValueError, ImportError) as exc:
            assert str(exc), f"{kind} failed with an empty message"


def test_resolving_an_agent_without_httpx_names_its_extra():
    """The resolver's import is lazy, so the hint has to survive to the caller.

    httpx cannot be uninstalled here — `mcp` pulls it in for everyone — so the
    absence is simulated the same way the sink tests do it: drop it from
    `sys.modules` and refuse the re-import.
    """
    from computeragent.harness.agentos_agent import AgentResolveError, resolve_agent

    removed = _strip_modules(["httpx"])
    try:

        class _Blocker:
            def find_spec(self, name, path=None, target=None):
                if name == "httpx" or name.startswith("httpx."):
                    raise ImportError(f"blocked {name} for test")
                return

        finder = _Blocker()
        sys.meta_path.insert(0, finder)
        try:
            with pytest.raises(AgentResolveError) as exc:
                resolve_agent(
                    "6aa93f1b3817a13d3103d2c1", base_url="http://h/agentos/api", api_key="k"
                )
            assert "computer-agent-py[agentos]" in str(exc.value)
        finally:
            sys.meta_path.remove(finder)
    finally:
        _restore(removed)


def test_http_session_store_without_httpx_names_its_extra():
    from computeragent.session_stores.http import HttpSessionStore

    removed = _strip_modules(["httpx"])
    try:

        class _Blocker:
            def find_spec(self, name, path=None, target=None):
                if name == "httpx" or name.startswith("httpx."):
                    raise ImportError(f"blocked {name} for test")
                return

        finder = _Blocker()
        sys.meta_path.insert(0, finder)
        try:
            with pytest.raises(ImportError) as exc:
                HttpSessionStore(base_url="https://h", token="t")
            assert "computer-agent-py[agentos]" in str(exc.value)
        finally:
            sys.meta_path.remove(finder)
    finally:
        _restore(removed)
