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
    "COMPUTERAGENT_AGENT_ID": AGENT_ID,
    "COMPUTERAGENT_ENGINE": "claude-agent-sdk",
    "COMPUTERAGENT_BASE_URL": BASE,
    "LYZR_API_KEY": "env-key",
    "COMPUTERAGENT_LLM_URL": LLM_URL,
    "COMPUTERAGENT_MCP_URL": "http://egress.internal/mcp",
    "COMPUTERAGENT_AGENT_API_URL": "http://egress.internal/agent-api",
    "COMPUTERAGENT_SRS_URL": "http://egress.internal/srs",
}


def resolve(env, **kw):
    params = dict(
        agent_id=None,
        engine=None,
        base_url=None,
        api_key=None,
        llm_url=None,
        mcp_url=None,
        agent_api_url=None,
        srs_url=None,
        gap_dir=None,
    )
    params.update(kw)
    return resolve_settings(env=env, **params)


def test_every_setting_has_an_env_var():
    assert ENV_VARS == {
        "agent_id": "COMPUTERAGENT_AGENT_ID",
        "engine": "COMPUTERAGENT_ENGINE",
        "base_url": "COMPUTERAGENT_BASE_URL",
        "api_key": "LYZR_API_KEY",
        "llm_url": "COMPUTERAGENT_LLM_URL",
        "mcp_url": "COMPUTERAGENT_MCP_URL",
        "agent_api_url": "COMPUTERAGENT_AGENT_API_URL",
        "srs_url": "COMPUTERAGENT_SRS_URL",
        "gap_dir": "COMPUTERAGENT_GAP_DIR",
    }


def test_env_vars_alone_are_enough():
    settings = resolve(FULL_ENV)

    assert settings.agent_id == AGENT_ID
    assert settings.engine == "claude-agent-sdk"
    assert settings.base_url == BASE
    assert settings.api_key() == "env-key"
    assert settings.llm_url == LLM_URL
    assert settings.mcp_url == "http://egress.internal/mcp"
    assert settings.agent_api_url == "http://egress.internal/agent-api"
    assert settings.srs_url == "http://egress.internal/srs"
    assert settings.gap_dir is None


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
    settings = resolve({**FULL_ENV, "COMPUTERAGENT_GAP_DIR": "/opt/agent"})

    assert settings.gap_dir == "/opt/agent"


def test_gap_dir_argument_wins(tmp_path):
    settings = resolve({**FULL_ENV, "COMPUTERAGENT_GAP_DIR": "/opt/agent"}, gap_dir=tmp_path)

    assert settings.gap_dir == tmp_path


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_value_counts_as_unset(blank):
    settings = resolve({**FULL_ENV, "COMPUTERAGENT_BASE_URL": blank}, base_url="http://arg")
    assert settings.base_url == "http://arg"

    with pytest.raises(SettingsError, match="base_url"):
        resolve({**FULL_ENV, "COMPUTERAGENT_BASE_URL": blank})


def test_every_missing_setting_is_reported_at_once():
    with pytest.raises(SettingsError) as exc:
        resolve({})

    message = str(exc.value)
    for name, var in ENV_VARS.items():
        if name == "gap_dir":
            assert name not in message
            continue
        assert f"{name} (or {var})" in message


def test_an_env_api_key_is_read_on_every_use():
    env = dict(FULL_ENV)
    settings = resolve(env)
    assert settings.api_key() == "env-key"

    env["LYZR_API_KEY"] = "rotated-key"

    assert settings.api_key() == "rotated-key"


def test_an_explicit_api_key_ignores_the_env_var():
    env = dict(FULL_ENV)
    settings = resolve(env, api_key="arg-key")

    env["LYZR_API_KEY"] = "rotated-key"

    assert settings.api_key() == "arg-key"


def test_an_api_key_removed_from_the_env_fails_when_used():
    env = dict(FULL_ENV)
    settings = resolve(env)
    del env["LYZR_API_KEY"]

    with pytest.raises(SettingsError, match="LYZR_API_KEY"):
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
    monkeypatch.setenv("COMPUTERAGENT_GAP_DIR", str(tmp_path))
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
        resolve({**FULL_ENV, "COMPUTERAGENT_LLM_URL": bad})
