"""The environment a gateway-bound agent's engine runs with.

This is the highest-value file in the suite: every assertion here stands in for
a live debugging session. The branches are asserted by exact dict equality
rather than per-key spot checks, because the failure mode that matters is an
*extra* or *renamed* variable, which a spot check cannot see.
"""

from __future__ import annotations

import base64

import pytest

from computeragent.harness.agentos_agent import ResolvedAgent
from computeragent.harness.gateway_envs import (
    GatewayConfigError,
    b64url,
    build_gateway_envs,
    build_options,
    dispatch_model,
    merge_envs,
    strip_provider_prefix,
)

KEY = "ags_testkey"
PROXY = "http://127.0.0.1:8786"
GATEWAY = "https://excellent-flavor-glance-myspace.trycloudflare.com"
STUDIO_ID = "6aa93f1a75ce74c4e6772b20"


def agent(**overrides) -> ResolvedAgent:
    defaults = dict(
        agent_id="6aa93f1b3817a13d3103d2c1",
        name="private git test with client",
        harness="claude-agent-sdk",
        source="https://github.com/vrajp-lyzr/general-agent-private",
        model=STUDIO_ID,
        gateway_type="native",
        gateway_base_url=GATEWAY,
        gateway_completions_url=f"{GATEWAY}/v4/chat/completions",
        credential_id=None,
        agent_type="client",
    )
    defaults.update(overrides)
    return ResolvedAgent(**defaults)  # type: ignore[arg-type]


# ── base64url ────────────────────────────────────────────────────────────────


def test_b64url_is_unpadded():
    """Node's `base64url` emits no `=`; the proxy decodes with the same
    alphabet, so padding is a wire difference, not a cosmetic one."""
    assert "=" not in b64url(GATEWAY)


def test_b64url_round_trips_through_the_url_alphabet():
    encoded = b64url(GATEWAY)
    padded = encoded + "=" * (-len(encoded) % 4)
    assert base64.urlsafe_b64decode(padded).decode() == GATEWAY


def test_b64url_uses_url_safe_characters():
    """A base whose standard-base64 encoding contains + and / must come back
    with - and _ instead, or the proxy's decode rejects the segment."""
    tricky = "https://a.example/~?>>>"
    encoded = b64url(tricky)
    assert "+" not in encoded and "/" not in encoded
    padded = encoded + "=" * (-len(encoded) % 4)
    assert base64.urlsafe_b64decode(padded).decode() == tricky


# ── branch 1: claude-agent-sdk + native gateway ──────────────────────────────


def test_claude_native_envs_exactly():
    envs = build_gateway_envs(
        agent(), engine_name="claude-agent-sdk", api_key=KEY, llm_proxy_url=PROXY
    )

    assert envs == {
        "ANTHROPIC_BASE_URL": f"{PROXY}/up/{b64url(GATEWAY)}",
        "GITAGENT_MODEL_BASE_URL": GATEWAY,
        "GITCLAW_MODEL_BASE_URL": GATEWAY,
        "ANTHROPIC_API_KEY": KEY,
        "OPENAI_API_KEY": KEY,
        "LYZR_API_KEY": KEY,
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }


def test_the_proxy_segment_encodes_the_bare_host_not_the_v4_path():
    """The single most likely regression here. The proxy appends its own
    LYZR_UPSTREAM_PATH, so encoding `{base}/v4` yields `/v4/v4` upstream."""
    envs = build_gateway_envs(
        agent(), engine_name="claude-agent-sdk", api_key=KEY, llm_proxy_url=PROXY
    )

    segment = envs["ANTHROPIC_BASE_URL"].rsplit("/up/", 1)[1]
    padded = segment + "=" * (-len(segment) % 4)
    assert base64.urlsafe_b64decode(padded).decode() == GATEWAY


def test_a_trailing_slash_on_the_proxy_url_does_not_double_up():
    envs = build_gateway_envs(
        agent(), engine_name="claude-agent-sdk", api_key=KEY, llm_proxy_url=PROXY + "/"
    )
    assert "//up/" not in envs["ANTHROPIC_BASE_URL"]


def test_claude_without_a_proxy_is_refused_by_name():
    with pytest.raises(GatewayConfigError, match="llm_proxy_url"):
        build_gateway_envs(agent(), engine_name="claude-agent-sdk", api_key=KEY)


# ── branch 2: gitagent + native gateway ──────────────────────────────────────


def test_gitagent_native_envs_exactly():
    """gitclaw speaks OpenAI directly — no proxy, and the base carries /v4
    because gitclaw appends only `/chat/completions` itself."""
    envs = build_gateway_envs(agent(harness="gitagent"), engine_name="gitagent", api_key=KEY)

    assert envs == {
        "ANTHROPIC_BASE_URL": f"{GATEWAY}/v4",
        "GITAGENT_MODEL_BASE_URL": f"{GATEWAY}/v4",
        "GITCLAW_MODEL_BASE_URL": f"{GATEWAY}/v4",
        "ANTHROPIC_API_KEY": KEY,
        "OPENAI_API_KEY": KEY,
        "LYZR_API_KEY": KEY,
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }


def test_gitagent_needs_no_proxy():
    build_gateway_envs(agent(harness="gitagent"), engine_name="gitagent", api_key=KEY)


# ── branch 3: no gateway ─────────────────────────────────────────────────────


def test_no_gateway_contributes_nothing():
    envs = build_gateway_envs(
        agent(gateway_type=None, gateway_base_url=None), engine_name="claude-agent-sdk", api_key=KEY
    )
    assert envs == {}


def test_no_gateway_needs_no_proxy_for_either_engine():
    bare = agent(gateway_type=None, gateway_base_url=None)
    assert build_gateway_envs(bare, engine_name="claude-agent-sdk", api_key=KEY) == {}
    assert build_gateway_envs(bare, engine_name="gitagent", api_key=KEY) == {}


def test_a_native_gateway_with_no_base_url_is_refused():
    with pytest.raises(GatewayConfigError, match="no baseUrl"):
        build_gateway_envs(
            agent(gateway_base_url=None),
            engine_name="claude-agent-sdk",
            api_key=KEY,
            llm_proxy_url=PROXY,
        )


# ── model ────────────────────────────────────────────────────────────────────


def test_studio_agent_id_reaches_the_claude_engine_untouched():
    assert dispatch_model(agent(), engine_name="claude-agent-sdk") == STUDIO_ID


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("Anthropic:anthropic/claude-sonnet-4-6", "anthropic/claude-sonnet-4-6"),
        ("openai:gpt-4o", "gpt-4o"),
        ("bedrock/us.anthropic.claude-sonnet-4-6", "bedrock/us.anthropic.claude-sonnet-4-6"),
        (STUDIO_ID, STUDIO_ID),
    ],
)
def test_provider_prefix_strip_matches_the_ts_engine(given, expected):
    assert strip_provider_prefix(given) == expected


def test_gitagent_gets_an_openai_prefix_so_it_finds_the_right_key():
    """gitclaw resolves its provider from the prefix; without one an
    OpenAI-shaped gateway model fails with 'No API key for provider'."""
    assert (
        dispatch_model(agent(harness="gitagent"), engine_name="gitagent") == f"openai:{STUDIO_ID}"
    )


def test_gitagent_does_not_double_prefix_a_model_that_has_one():
    a = agent(harness="gitagent", model="anthropic:claude-sonnet-4-6")
    assert dispatch_model(a, engine_name="gitagent") == "anthropic:claude-sonnet-4-6"


def test_gitagent_without_a_gateway_leaves_the_model_alone():
    a = agent(harness="gitagent", gateway_type=None, gateway_base_url=None, model="gpt-4o")
    assert dispatch_model(a, engine_name="gitagent") == "gpt-4o"


def test_no_model_stays_none():
    assert dispatch_model(agent(model=None), engine_name="claude-agent-sdk") is None


# ── env merge ────────────────────────────────────────────────────────────────


def test_gateway_envs_beat_caller_envs():
    merged = merge_envs(
        {"ANTHROPIC_API_KEY": "caller-key", "MY_VAR": "kept"},
        build_gateway_envs(
            agent(), engine_name="claude-agent-sdk", api_key=KEY, llm_proxy_url=PROXY
        ),
    )

    assert merged["ANTHROPIC_API_KEY"] == KEY
    assert merged["MY_VAR"] == "kept"


def test_bedrock_flag_is_dropped_when_a_gateway_resolved():
    """The CLI routes via Bedrock whenever this is set and ignores
    ANTHROPIC_BASE_URL entirely — a gateway agent would silently send its model
    id to Bedrock and be rejected as an invalid Bedrock model."""
    merged = merge_envs(
        {"CLAUDE_CODE_USE_BEDROCK": "1"},
        build_gateway_envs(
            agent(), engine_name="claude-agent-sdk", api_key=KEY, llm_proxy_url=PROXY
        ),
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
    assert options["model"] == STUDIO_ID


def test_caller_options_win():
    options = build_options(
        agent(),
        engine_name="claude-agent-sdk",
        caller_options={"permission_mode": "default", "system_prompt": "You are terse."},
    )

    assert options["permission_mode"] == "default"
    assert options["system_prompt"] == "You are terse."
    assert options["model"] == STUDIO_ID


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
