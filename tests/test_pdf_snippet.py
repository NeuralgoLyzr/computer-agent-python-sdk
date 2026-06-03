"""Verbatim NordAssist PDF page 4-5 snippet with only the import line swapped.

If this passes, the drop-in promise is real.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# The two import-line swaps — every other line below is identical to the PDF.
from computeragent import ClaudeAgentOptions, query
from computeragent.types import ResultMessage


@pytest.mark.asyncio
async def test_nordassist_snippet_drop_in(upstream_query_factory):
    # Build a canned ResultMessage matching the upstream shape. The fields
    # NordAssist reads are `.result`, `.num_turns`, `.total_cost_usd`.
    canned_result = MagicMock(spec=ResultMessage)
    canned_result.result = "Answer: routing happens in route_question (BM25)."
    canned_result.num_turns = 3
    canned_result.total_cost_usd = 0.0123
    canned_result.session_id = "sess_abc"
    canned_result.is_error = False
    canned_result.subtype = "success"
    canned_result.usage = {"input_tokens": 100, "output_tokens": 50}
    canned_result.duration_api_ms = 750.0
    canned_result.model = "us.anthropic.claude-sonnet-4-6"
    # Make isinstance(canned_result, ResultMessage) return True (MagicMock
    # spec already arranges this).

    upstream_query_factory([canned_result])

    # ── verbatim PDF page-4 / page-5 snippet ──────────────────────────────
    options = ClaudeAgentOptions(
        model="us.anthropic.claude-sonnet-4-6",
        cwd="/tmp/session-fake",
        system_prompt=(
            "You are NordAssist, an AI assistant for engineers. "
            "Read CLAUDE.md for your instructions and available documentation."
        ),
        allowed_tools=["Read", "Glob", "Grep", "mcp__nordassist-tools__*"],
        mcp_servers={},  # empty for the unit test
        permission_mode="bypassPermissions",
        add_dirs=["/tmp/docs-fake"],
        env={"AWS_REGION": "us-east-1"},
    )

    question = "How does routing work?"

    query_stream = query(prompt=question, options=options)
    answer = ""
    turns = 0
    cost = 0.0
    async for message in query_stream:
        if isinstance(message, ResultMessage):
            answer = message.result or ""
            turns = message.num_turns or 0
            cost = message.total_cost_usd or 0.0
    # ──────────────────────────────────────────────────────────────────────

    assert answer.startswith("Answer:")
    assert turns == 3
    assert cost == pytest.approx(0.0123)
