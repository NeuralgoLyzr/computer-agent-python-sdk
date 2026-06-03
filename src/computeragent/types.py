"""Re-export of ``claude_agent_sdk.types``.

Every type that ``claude_agent_sdk.types`` exposes is re-exported here with
identity preserved. This is what makes ``isinstance(msg, ResultMessage)`` work
whether the user imported ``ResultMessage`` from ``claude_agent_sdk.types`` or
from ``computeragent.types``.

Switching the import line is the only change a drop-in user needs.
"""

from __future__ import annotations

from claude_agent_sdk.types import *  # noqa: F401, F403
