"""``LocalSubstrate`` — provides a workspace ``cwd`` for the engine.

Resolution rules:

* :class:`pathlib.Path` — used directly (caller-owned; not deleted on close).
* String — coerced to :class:`Path` and used directly.
* Dict (inline manifest) — the current working directory is used.

Nothing is fetched or cloned: the agent is baked into the image the SDK runs
in, so every workspace is a directory that already exists and outlives the run.

No subprocess management lives here. ``claude-agent-sdk`` and
``GitAgentEngine`` own their own model-call subprocesses and just need a
``cwd`` to point at.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

from ..protocol_types import IdentitySource, SubstrateHandle

logger = logging.getLogger("computeragent.substrates")


class LocalSubstrate:
    name = "local"

    async def boot(
        self,
        *,
        source: IdentitySource,
        envs: Mapping[str, str],  # noqa: ARG002 - reserved for parity with remote substrates
    ) -> SubstrateHandle:
        if isinstance(source, Path):
            return SubstrateHandle(workspace_path=source, close=_noop_close)

        if isinstance(source, str):
            return SubstrateHandle(workspace_path=Path(source), close=_noop_close)

        if isinstance(source, dict):
            return SubstrateHandle(workspace_path=Path.cwd(), close=_noop_close)

        raise TypeError(f"LocalSubstrate.boot: unsupported source type {type(source).__name__}")


async def _noop_close() -> None:
    return None


__all__ = ["LocalSubstrate"]
