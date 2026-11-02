"""Every ComputerAgent setting resolves from its argument or its env var.

The argument wins, a blank value counts as unset, every missing required
setting is reported in one error naming both spellings, and an ``api_key`` from
the environment is read again on every use.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from computeragent.harness import ComputerAgent
from computeragent.harness.settings import ENV_VARS, SettingsError, resolve_settings

AGENT_ID = "6aa93f1b3817a13d3103d2c1"
BASE = "http://localhost:8788/agentos/api"
URL = f"{BASE}/v1/agents/{AGENT_ID}"
LLM_URL = "http://egress.internal/llm"

FULL_ENV = {
    "COMPUTER_AGENT_ID": AGENT_ID,
    "COMPUTER_AGENT_ENGINE": "claude-agent-sdk",
    "COMPUTER_AGENT_EGRESS_API_URL": BASE,
    "COMPUTER_AGENT_EGRESS_PROXY_SECRET": "env-key",
    "COMPUTER_AGENT_EGRESS_LLM_URL": LLM_URL,
    "COMPUTER_AGENT_EGRESS_MCP_URL": "http://egress.internal/mcp",
    "COMPUTER_AGENT_EGRESS_GUARDRAILS_URL": "http://egress.internal/guardrails",
}


def resolve(env, **kw):
    params = dict(
        agent_id=None,
        engine=None,
        base_url=None,
        api_key=None,
        llm_url=None,
        mcp_url=None,
        guardrails_url=None,
        telemetry_url=None,
        gap_dir=None,
    )
    params.update(kw)
    return resolve_settings(env=env, **params)


def test_every_setting_has_an_env_var():
    assert ENV_VARS == {
        "agent_id": "COMPUTER_AGENT_ID",
        "engine": "COMPUTER_AGENT_ENGINE",
        "base_url": "COMPUTER_AGENT_EGRESS_API_URL",
        "api_key": "COMPUTER_AGENT_EGRESS_PROXY_SECRET",
        "llm_url": "COMPUTER_AGENT_EGRESS_LLM_URL",
        "mcp_url": "COMPUTER_AGENT_EGRESS_MCP_URL",
        "guardrails_url": "COMPUTER_AGENT_EGRESS_GUARDRAILS_URL",
        "telemetry_url": "COMPUTER_AGENT_EGRESS_TELEMETRY_URL",
        "session_store_url": "COMPUTER_AGENT_EGRESS_SESSION_STORE_URL",
        "gap_dir": "COMPUTER_AGENT_GAP_DIR",
        "guardrails_timeout_s": "COMPUTER_AGENT_GUARDRAILS_TIMEOUT_S",
    }


def test_env_vars_alone_are_enough():
    settings = resolve(FULL_ENV)

    assert settings.agent_id == AGENT_ID
    assert settings.engine == "claude-agent-sdk"
    assert settings.base_url == BASE
    assert settings.api_key() == "env-key"
    assert settings.llm_url == LLM_URL
    assert settings.mcp_url == "http://egress.internal/mcp"
    assert settings.guardrails_url == "http://egress.internal/guardrails"
    assert settings.gap_dir is None


def test_the_old_srs_and_agent_api_env_vars_are_not_read():
    env = {
        **{k: v for k, v in FULL_ENV.items() if k != "COMPUTER_AGENT_EGRESS_GUARDRAILS_URL"},
        "COMPUTERAGENT_SRS_URL": "http://egress.internal/srs",
        "AGENT_API_URL": "http://egress.internal/agent-api",
        "COMPUTERAGENT_AGENT_API_URL": "http://egress.internal/agent-api",
    }

    with pytest.raises(SettingsError, match="guardrails_url"):
        resolve(env)


def test_the_argument_wins_over_the_env_var():
    settings = resolve(
        FULL_ENV, agent_id="arg-id", engine="gitagent", base_url="http://arg", api_key="arg-key"
    )

    assert settings.agent_id == "arg-id"
    assert settings.engine == "gitagent"
    assert settings.base_url == "http://arg"
    assert settings.api_key() == "arg-key"


def test_an_engine_instance_is_kept_as_is():
    engine = object()

    assert resolve(FULL_ENV, engine=engine).engine is engine


def test_gap_dir_comes_from_its_env_var():
    settings = resolve({**FULL_ENV, "COMPUTER_AGENT_GAP_DIR": "/opt/agent"})

    assert settings.gap_dir == "/opt/agent"


def test_gap_dir_argument_wins(tmp_path):
    settings = resolve({**FULL_ENV, "COMPUTER_AGENT_GAP_DIR": "/opt/agent"}, gap_dir=tmp_path)

    assert settings.gap_dir == tmp_path


def test_the_guardrails_timeout_defaults_to_60_seconds():
    assert resolve(FULL_ENV).guardrails_timeout_s == 60.0


def test_the_guardrails_timeout_comes_from_its_env_var_and_the_argument_wins():
    env = {**FULL_ENV, "COMPUTER_AGENT_GUARDRAILS_TIMEOUT_S": " 12.5 "}

    assert resolve(env).guardrails_timeout_s == 12.5
    assert resolve(env, guardrails_timeout_s=3).guardrails_timeout_s == 3.0


@pytest.mark.parametrize("value", ["abc", "0", "-1", "inf", "nan"])
def test_a_bad_guardrails_timeout_env_var_is_refused(value):
    with pytest.raises(SettingsError, match="COMPUTER_AGENT_GUARDRAILS_TIMEOUT_S"):
        resolve({**FULL_ENV, "COMPUTER_AGENT_GUARDRAILS_TIMEOUT_S": value})


@pytest.mark.parametrize("value", [0, -5.0, True])
def test_a_bad_guardrails_timeout_argument_is_refused(value):
    with pytest.raises(SettingsError, match="guardrails_timeout_s"):
        resolve(FULL_ENV, guardrails_timeout_s=value)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_value_counts_as_unset(blank):
    settings = resolve({**FULL_ENV, "COMPUTER_AGENT_EGRESS_API_URL": blank}, base_url="http://arg")
    assert settings.base_url == "http://arg"

    with pytest.raises(SettingsError, match="base_url"):
        resolve({**FULL_ENV, "COMPUTER_AGENT_EGRESS_API_URL": blank})


def test_every_missing_setting_is_reported_at_once():
    with pytest.raises(SettingsError) as exc:
        resolve({})

    message = str(exc.value)
    for name, var in ENV_VARS.items():
        if name in ("gap_dir", "telemetry_url", "session_store_url", "guardrails_timeout_s"):
            assert name not in message
            continue
        assert f"{name} (or {var})" in message


def test_an_env_api_key_is_read_on_every_use():
    env = dict(FULL_ENV)
    settings = resolve(env)
    assert settings.api_key() == "env-key"

    env["COMPUTER_AGENT_EGRESS_PROXY_SECRET"] = "rotated-key"

    assert settings.api_key() == "rotated-key"


def test_an_explicit_api_key_ignores_the_env_var():
    env = dict(FULL_ENV)
    settings = resolve(env, api_key="arg-key")

    env["COMPUTER_AGENT_EGRESS_PROXY_SECRET"] = "rotated-key"

    assert settings.api_key() == "arg-key"


def test_an_api_key_removed_from_the_env_fails_when_used():
    env = dict(FULL_ENV)
    settings = resolve(env)
    del env["COMPUTER_AGENT_EGRESS_PROXY_SECRET"]

    with pytest.raises(SettingsError, match="COMPUTER_AGENT_EGRESS_PROXY_SECRET"):
        settings.api_key()


def test_repr_never_shows_the_key():
    settings = resolve(FULL_ENV, api_key="sk-secret-value")

    assert "sk-secret-value" not in repr(settings)
    assert "env-key" not in repr(resolve(FULL_ENV))


@respx.mock
def test_computeragent_builds_from_env_vars_alone(agent_doc, monkeypatch):
    for name, value in FULL_ENV.items():
        monkeypatch.setenv(name, value)
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    agent = ComputerAgent()

    assert agent.agent.agent_id == AGENT_ID
    assert route.calls.last.request.headers["x-api-key"] == "env-key"
    assert agent._coordinator._engine.name == "claude-agent-sdk"  # type: ignore[attr-defined]


@respx.mock
def test_computeragent_gap_dir_from_env(agent_doc, monkeypatch, tmp_path):
    (tmp_path / "agent.yaml").write_text("name: fixture\n", encoding="utf-8")
    for name, value in FULL_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("COMPUTER_AGENT_GAP_DIR", str(tmp_path))
    respx.get(URL).mock(return_value=httpx.Response(200, json=agent_doc))

    agent = ComputerAgent()

    assert agent._coordinator._source == Path(tmp_path)  # type: ignore[attr-defined]


def test_computeragent_reports_missing_settings_before_any_request():
    with pytest.raises(SettingsError, match="agent_id"):
        ComputerAgent()


def test_llm_url_argument_wins_and_loses_its_trailing_slash():
    settings = resolve(FULL_ENV, llm_url="https://other/llm/")

    assert settings.llm_url == "https://other/llm"


@pytest.mark.parametrize("bad", ["egress/llm", "ftp://egress/llm"])
def test_a_url_setting_must_be_http(bad):
    with pytest.raises(SettingsError, match="http\\(s\\) URLs: llm_url"):
        resolve({**FULL_ENV, "COMPUTER_AGENT_EGRESS_LLM_URL": bad})


def test_the_telemetry_url_is_optional():
    assert resolve(FULL_ENV).telemetry_url is None


def test_the_telemetry_url_comes_from_its_env_var():
    settings = resolve({**FULL_ENV, "COMPUTER_AGENT_EGRESS_TELEMETRY_URL": "http://egress/otlp/"})

    assert settings.telemetry_url == "http://egress/otlp"


def test_an_optional_url_must_still_be_http_when_set():
    with pytest.raises(SettingsError, match="telemetry_url"):
        resolve({**FULL_ENV, "COMPUTER_AGENT_EGRESS_TELEMETRY_URL": "egress/otlp"})


def test_events_url_is_unknown():
    """The dashboard events sink is gone: its variable is not read and the
    resolver takes no events_url."""
    settings = resolve({**FULL_ENV, "COMPUTERAGENT_EVENTS_URL": "http://egress/ingest"})

    assert not hasattr(settings, "events_url")
    with pytest.raises(TypeError, match="events_url"):
        resolve(FULL_ENV, events_url="http://egress/ingest")


@pytest.mark.parametrize("explicit", [None, "sbt_root"])
def test_api_key_resolves_the_chain(explicit):
    from datetime import datetime, timedelta, timezone

    from computeragent.harness.egress_token import CREDENTIAL

    env = {**FULL_ENV, "COMPUTER_AGENT_EGRESS_PROXY_SECRET": "sbt_root"}
    settings = resolve(env, api_key=explicit)
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    CREDENTIAL.adopt("sbt_root", "sbt_next", expiry)

    assert settings.api_key() == "sbt_next"


def test_session_store_url():
    assert resolve(FULL_ENV).session_store_url is None
    assert (
        resolve(
            {
                **FULL_ENV,
                "COMPUTER_AGENT_EGRESS_SESSION_STORE_URL": "http://egress/computer-agent-api/",
            }
        ).session_store_url
        == "http://egress/computer-agent-api"
    )


@pytest.mark.parametrize("bad", ["egress/x", "ftp://x", "http://e/x?y=1", "http://e/x#f"])
def test_session_store_url_is_validated(bad):
    with pytest.raises(SettingsError, match="session_store_url") as exc:
        resolve(FULL_ENV, session_store_url=bad)
    assert bad not in str(exc.value)
