"""``PassthroughLoader`` — treats the source as the manifest itself.

Resolution rules:

* ``dict`` source → fields lift straight into :class:`AgentManifest`
  (``name``, ``version``, ``options``, ``sha``).
* ``Path`` / ``str`` source → name = ``workspace_path.name``,
  options = ``{}``. The engine is expected to pick up its own defaults
  (e.g. a CLAUDE.md in the workspace).

GAP-style git-repo loaders that fetch manifests over the network land
in 0.3.0.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..protocol_types import AgentManifest, IdentitySource


class PassthroughLoader:
    name = "passthrough"

    async def load(
        self,
        *,
        source: IdentitySource,
        target_engine: str,  # noqa: ARG002 - reserved for engine-specific manifest shaping
        workspace_path: Path,
    ) -> AgentManifest:
        if isinstance(source, dict):
            name = source.get("name") or _default_name(workspace_path)
            return AgentManifest(
                name=name,
                version=source.get("version", "0.0.0"),
                options=_coerce_options(source.get("options")),
                sha=source.get("sha"),
            )
        return AgentManifest(
            name=_default_name(workspace_path),
            version="0.0.0",
            options={},
        )


def _default_name(workspace_path: Path) -> str:
    try:
        return workspace_path.resolve().name or "computeragent"
    except OSError:
        return "computeragent"


def _coerce_options(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(
        f"AgentManifest.options must be a Mapping; got {type(value).__name__}"
    )


__all__ = ["PassthroughLoader"]
