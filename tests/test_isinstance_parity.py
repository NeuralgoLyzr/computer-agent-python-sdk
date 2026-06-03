"""Identity-preserving re-export — the load-bearing property for drop-in compat."""

from __future__ import annotations

import claude_agent_sdk
from claude_agent_sdk.types import ResultMessage as UpstreamResult
from claude_agent_sdk.types import TextBlock as UpstreamText

import computeragent
import computeragent.types as ours


def test_result_message_identity():
    assert computeragent.ResultMessage is UpstreamResult
    assert ours.ResultMessage is UpstreamResult


def test_text_block_identity():
    assert computeragent.TextBlock is UpstreamText


def test_isinstance_check_works_across_import_paths():
    """The whole reason for identity preservation: ``isinstance`` is the
    contract NordAssist relies on, and it must agree no matter which import
    name space the user picked.
    """
    # Build a real upstream object (don't construct ResultMessage directly —
    # its required fields vary; use a TextBlock which is simpler).
    block = UpstreamText(text="hello")
    assert isinstance(block, computeragent.TextBlock)
    assert isinstance(block, ours.TextBlock)
    assert isinstance(block, claude_agent_sdk.TextBlock)


def test_options_dataclass_is_shared():
    opts1 = computeragent.ClaudeAgentOptions(model="m")
    opts2 = claude_agent_sdk.ClaudeAgentOptions(model="m")
    assert type(opts1) is type(opts2)
