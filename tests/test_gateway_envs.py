"""The environment an agent's engine runs with.

Every model call goes to ``llm_url``. The envs are asserted by exact dict
equality rather than per-key spot checks, because the failure mode that matters
is an *extra* or *renamed* variable, which a spot check cannot see.
"""

from __future__ import annotations

import pytest

from computeragent.harness.agentos_agent import ResolvedAgent
from computeragent.harness.gateway_envs import (
    GatewayConfigError,
    build_gateway_envs,
    build_options,
    dispatch_model,
    key_envs,
    merge_envs,
    strip_provider_prefix,
)

KEY = "ags_testkey"
LLM_URL = "http://egress.internal/llm"
KONG_MODEL = "claude-sonnet-4-5"


def agent(**overrides) -> ResolvedAgent:
    defaults = dict(
        agent_id="6aa93f1b3817a13d3103d2c1",
        name="private git test with client",
        harness="claude-agent-sdk",
        model=KONG_MODEL,
        agent_type="client",
    )
    defaults.update(overrides)
    return ResolvedAgent(**defaults)  # type: ignore[arg-type]


# ── llm_url ──────────────────────────────────────────────────────────────────


def expected_envs(base: str = LLM_URL) -> dict[str, str]:
    return {
        "ANTHROPIC_BASE_URL": base,
        "GITAGENT_MODEL_BASE_URL": base,
        "GITCLAW_MODEL_BASE_URL": base,
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }


def test_llm_url_envs_exactly():
    assert build_gateway_envs(llm_url=LLM_URL) == expected_envs()


def test_llm_url_is_passed_verbatim_with_no_proxy_or_version_segments():
    """The egress proxy owns its routes: no /up/<b64>, no /v4 appended."""
    envs = build_gateway_envs(llm_url=LLM_URL)

    for name in ("ANTHROPIC_BASE_URL", "GITCLAW_MODEL_BASE_URL", "GITAGENT_MODEL_BASE_URL"):
        assert envs[name] == LLM_URL
        assert "/up/" not in envs[name]
        assert not envs[name].endswith("/v4")


def test_a_trailing_slash_on_llm_url_is_dropped():
    assert build_gateway_envs(llm_url=LLM_URL + "/") == expected_envs()


def test_the_cli_background_traffic_is_switched_off():
    """The Claude CLI otherwise talks to Anthropic and Datadog itself, around
    the egress proxy."""
    envs = build_gateway_envs(llm_url=LLM_URL)

    assert envs["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert envs["DISABLE_AUTOUPDATER"] == "1"


@pytest.mark.parametrize("bad", ["", "egress.internal/llm", "ftp://egress/llm", None])
def test_a_malformed_llm_url_is_refused(bad):
    with pytest.raises(GatewayConfigError, match="llm_url"):
        build_gateway_envs(llm_url=bad)  # type: ignore[arg-type]


# ── model ────────────────────────────────────────────────────────────────────


def test_kong_model_reaches_the_claude_engine_untouched():
    assert dispatch_model(agent(), engine_name="claude-agent-sdk") == KONG_MODEL


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("Anthropic:anthropic/claude-sonnet-4-6", "anthropic/claude-sonnet-4-6"),
        ("openai:gpt-4o", "gpt-4o"),
        ("bedrock/us.anthropic.claude-sonnet-4-6", "bedrock/us.anthropic.claude-sonnet-4-6"),
        (KONG_MODEL, KONG_MODEL),
    ],
)
def test_provider_prefix_strip_matches_the_ts_engine(given, expected):
    assert strip_provider_prefix(given) == expected


def test_gitagent_gets_an_openai_prefix_so_it_finds_the_right_key():
    """gitclaw resolves its provider from the prefix; without one an
    OpenAI-shaped gateway model fails with 'No API key for provider'."""
    assert (
        dispatch_model(agent(harness="gitagent"), engine_name="gitagent") == f"openai:{KONG_MODEL}"
    )


def test_gitagent_does_not_double_prefix_a_model_that_has_one():
    a = agent(harness="gitagent", model="anthropic:claude-sonnet-4-6")
    assert dispatch_model(a, engine_name="gitagent") == "anthropic:claude-sonnet-4-6"


def test_no_model_stays_none():
    assert dispatch_model(agent(model=None), engine_name="claude-agent-sdk") is None


# ── env merge ────────────────────────────────────────────────────────────────


def test_key_envs():
    assert key_envs("k") == {"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", "LYZR_API_KEY": "k"}
    assert not set(key_envs("k")) & set(build_gateway_envs(llm_url=LLM_URL))


def test_gateway_and_turn_key_envs_beat_caller_envs():
    merged = merge_envs(
        {"ANTHROPIC_API_KEY": "caller-key", "ANTHROPIC_BASE_URL": "http://x", "MY_VAR": "kept"},
        build_gateway_envs(llm_url=LLM_URL),
    )
    turn = {**merged, **key_envs(KEY)}  # what the coordinator hands the engine

    assert turn["ANTHROPIC_API_KEY"] == KEY
    assert turn["ANTHROPIC_BASE_URL"] == LLM_URL
    assert turn["MY_VAR"] == "kept"


def test_bedrock_flag_is_dropped():
    """The CLI routes via Bedrock whenever this is set and ignores
    ANTHROPIC_BASE_URL entirely, which would send model calls around the
    egress proxy."""
    merged = merge_envs(
        {"CLAUDE_CODE_USE_BEDROCK": "1"},
        build_gateway_envs(llm_url=LLM_URL),
    )

    assert "CLAUDE_CODE_USE_BEDROCK" not in merged


def test_bedrock_flag_survives_when_there_is_no_gateway():
    merged = merge_envs({"CLAUDE_CODE_USE_BEDROCK": "1"}, {})
    assert merged["CLAUDE_CODE_USE_BEDROCK"] == "1"


def test_merge_tolerates_no_caller_envs():
    assert merge_envs(None, {"A": "1"}) == {"A": "1"}


# ── options ──────────────────────────────────────────────────────────────────


def test_options_carry_what_agentos_dispatches():
    options = build_options(agent(), engine_name="claude-agent-sdk")

    assert options["permission_mode"] == "bypassPermissions"
    assert options["setting_sources"] == ["project"]
    assert options["model"] == KONG_MODEL


def test_caller_options_win():
    options = build_options(
        agent(),
        engine_name="claude-agent-sdk",
        caller_options={"permission_mode": "default", "system_prompt": "You are terse."},
    )

    assert options["permission_mode"] == "default"
    assert options["system_prompt"] == "You are terse."
    assert options["model"] == KONG_MODEL


def test_caller_can_override_the_resolved_model():
    options = build_options(
        agent(), engine_name="claude-agent-sdk", caller_options={"model": "claude-haiku-4-5"}
    )
    assert options["model"] == "claude-haiku-4-5"


def test_no_model_key_when_nothing_is_bound():
    """Leaving the key out lets the GAP repo's agent.yaml decide, rather than
    pinning the model to None."""
    options = build_options(agent(model=None), engine_name="claude-agent-sdk")
    assert "model" not in options
