"""Where the roster contract is appended, per engine shape."""

from __future__ import annotations

from computeragent.orchestration.engine_shape import is_gitclaw_shaped
from computeragent.orchestration.roster_contract import (
    ROSTER_CONTRACT,
    append_roster_contract,
)


def test_the_contract_states_the_missing_clause_every_prompt_omits():
    assert "NEVER emit a roster with an empty worker list" in ROSTER_CONTRACT
    assert "Answer the user directly in plain prose" in ROSTER_CONTRACT


def test_the_claude_preset_object_is_extended_not_replaced():
    options = {
        "system_prompt": {
            "type": "preset",
            "preset": "claude_code",
            "append": "# Soul\n\nbe helpful",
        },
        "model": "m",
    }
    out = append_roster_contract(options, "claude-agent-sdk")
    # The GAP repo's SOUL/RULES text must survive.
    assert out["system_prompt"]["append"].startswith("# Soul")
    assert ROSTER_CONTRACT in out["system_prompt"]["append"]
    assert out["system_prompt"]["preset"] == "claude_code"
    assert out["model"] == "m"


def test_an_empty_append_does_not_gain_a_leading_blank_line():
    out = append_roster_contract(
        {"system_prompt": {"type": "preset", "preset": "claude_code"}}, "claude-agent-sdk"
    )
    assert out["system_prompt"]["append"] == ROSTER_CONTRACT


def test_a_bare_string_prompt_is_concatenated():
    out = append_roster_contract({"system_prompt": "you are an agent"}, "claude-agent-sdk")
    assert out["system_prompt"] == f"you are an agent\n\n{ROSTER_CONTRACT}"


def test_no_prompt_at_all_gains_the_preset_shape_the_engine_expects():
    out = append_roster_contract({}, "claude-agent-sdk")
    assert out["system_prompt"] == {
        "type": "preset",
        "preset": "claude_code",
        "append": ROSTER_CONTRACT,
    }


def test_gitclaw_appends_onto_its_own_string_prompt():
    # This SDK's gitagent engine reads `system_prompt` and nothing else, so a
    # `system_prompt_suffix` key would be silently dropped. Concatenating is
    # the only append available, and it preserves the loader's prompt.
    out = append_roster_contract({"system_prompt": "gap prompt"}, "gitagent")
    assert out["system_prompt"] == f"gap prompt\n\n{ROSTER_CONTRACT}"
    assert "system_prompt_suffix" not in out


def test_gitclaw_with_no_prompt_gets_the_contract_alone_not_a_preset_object():
    out = append_roster_contract({}, "gitagent")
    assert out["system_prompt"] == ROSTER_CONTRACT


def test_a_non_dict_options_bag_is_returned_untouched():
    # A loader that produced nothing must never be turned INTO something here.
    assert append_roster_contract(None, "claude-agent-sdk") is None
    assert append_roster_contract(["a"], "claude-agent-sdk") == ["a"]


def test_the_original_options_bag_is_not_mutated():
    options = {"system_prompt": "x"}
    append_roster_contract(options, "claude-agent-sdk")
    assert options == {"system_prompt": "x"}


def test_only_gitagent_is_gitclaw_shaped():
    assert is_gitclaw_shaped("gitagent") is True
    assert is_gitclaw_shaped("claude-agent-sdk") is False
