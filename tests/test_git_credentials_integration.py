"""End-to-end private-GAP clone against a REAL AgentOS server.

Proves the whole chain the design hinges on:
  SDK's AgentOS API key  →  /git-credentials/resolve  →  the key's GROUP's PAT
  →  authenticated `git clone` of a PRIVATE repo, locally, with a captured SHA.

Skipped unless you point it at a live AgentOS + a private repo the key's group
has a credential for. Run with::

    AGENTOS_API_URL=https://<host>/agentos/api/v1 \
    COMPUTERAGENT_HARNESS_TOKEN=cak_... \
    CA_TEST_PRIVATE_REPO=https://github.com/<org>/<private-repo> \
        uv run pytest -m integration tests/test_git_credentials_integration.py

Prereqs on the server side:
  * a git credential exists for (the key's group, the repo's host)
  * the key's role includes `git-credentials:read`
  * AGENTOS_CREDENTIALS_KEY is set on the server
"""

from __future__ import annotations

import os
import re

import pytest

from computeragent.harness.git_credential_client import resolve_git_credential
from computeragent.substrates import LocalSubstrate

_REPO = os.environ.get("CA_TEST_PRIVATE_REPO", "")
_HAVE_CFG = bool(
    os.environ.get("AGENTOS_API_URL")
    and (os.environ.get("COMPUTERAGENT_HARNESS_TOKEN") or os.environ.get("AGENTOS_INGEST_TOKEN"))
    and _REPO
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _HAVE_CFG,
        reason="set AGENTOS_API_URL + COMPUTERAGENT_HARNESS_TOKEN + CA_TEST_PRIVATE_REPO to run",
    ),
]


@pytest.mark.asyncio
async def test_resolve_returns_group_pat():
    """The API key resolves to its group's PAT for the repo host."""
    cred = await resolve_git_credential(_REPO)
    assert cred is not None, (
        "AgentOS returned no credential — check the key's group has a git "
        "credential for this host, the key's role has git-credentials:read, "
        "and AGENTOS_API_URL/token are correct."
    )
    assert cred["token"], "resolve returned an empty token"
    assert cred["host"] in _REPO


@pytest.mark.asyncio
async def test_local_clone_of_private_repo_with_resolved_pat():
    """Full path: resolve the PAT, then clone the private repo locally with it."""
    cred = await resolve_git_credential(_REPO)
    assert cred is not None, "no credential resolved (see test_resolve_returns_group_pat)"

    sub = LocalSubstrate()
    handle = await sub.boot(source=_REPO, envs={}, git_credential=cred)
    try:
        assert handle.workspace_path.exists()
        assert any(handle.workspace_path.iterdir()), "private repo cloned empty — auth likely failed"
        assert handle.revision and re.fullmatch(r"[0-9a-f]{40}", handle.revision)
    finally:
        await handle.close()
    assert not handle.workspace_path.exists()  # close() cleans up the temp clone
