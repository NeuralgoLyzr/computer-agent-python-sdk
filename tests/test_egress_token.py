"""Reissued sandbox tokens: when one is adopted, and how every client picks it up."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from computeragent.harness.egress_token import (
    CREDENTIAL,
    HEADER_EXPIRES_AT,
    HEADER_TOKEN,
    EgressCredential,
    egress_event_hooks,
    install,
    stamping_session,
)
from computeragent.harness.settings import resolve_settings

EGRESS = "http://egress.test"


def later(hours: float = 4) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def cred(env=None) -> EgressCredential:
    c = EgressCredential(env={} if env is None else env)
    c.configure([f"{EGRESS}/llm"])
    return c


# ── adoption ─────────────────────────────────────────────────────────────────


def test_adopts_later_expiry():
    env = {"COMPUTER_AGENT_EGRESS_PROXY_SECRET": "sbt_a"}
    c = cred(env)

    assert c.adopt("sbt_a", "sbt_b", later(4)) is True
    assert c.resolve("sbt_a") == c.resolve("sbt_b") == "sbt_b"
    assert env["COMPUTER_AGENT_EGRESS_PROXY_SECRET"] == "sbt_b"

    assert c.adopt("sbt_b", "sbt_c", later(5)) is True
    assert {c.resolve(t) for t in ("sbt_a", "sbt_b", "sbt_c")} == {"sbt_c"}
    assert env["COMPUTER_AGENT_EGRESS_PROXY_SECRET"] == "sbt_c"


@pytest.mark.parametrize(
    ("token", "expires_at"),
    [
        (None, later()),
        ("sbt_b", None),
        ("tok_b", later()),
        ("sbt_" + "x" * 4093, later()),
        ("sbt_b", "tomorrow"),
        ("sbt_b", "2099-01-01T00:00:00"),  # no zone
        ("sbt_b", later(-1)),
    ],
)
def test_rejects(token, expires_at):
    c = cred()

    assert c.adopt("sbt_a", token, expires_at) is False
    assert c.resolve("sbt_a") == "sbt_a"


def test_rejects_a_reissue_on_a_superseded_token():
    c = cred()
    c.adopt("sbt_a", "sbt_b", later(4))

    assert c.adopt("sbt_a", "sbt_x", later(6)) is False
    assert c.resolve("sbt_a") == "sbt_b"


def test_rejects_an_expiry_that_is_not_later():
    c = cred()
    c.adopt("sbt_a", "sbt_b", later(4))

    assert c.adopt("sbt_b", "sbt_c", later(3)) is False
    assert c.resolve("sbt_a") == "sbt_b"


def test_a_chain_whose_newest_token_expired_is_dropped():
    c = cred()
    c.adopt("sbt_a", "sbt_b", later(4))
    chain = c._chains["sbt_a"]
    chain.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert c.resolve("sbt_a") == "sbt_a"


def test_new_root_is_not_rolled_back(monkeypatch):
    """The sandbox's next turn brings a fresh key; a late reissue of the old
    chain only extends the old chain."""
    monkeypatch.setenv("COMPUTER_AGENT_EGRESS_PROXY_SECRET", "sbt_1")
    CREDENTIAL.configure([f"{EGRESS}/llm"])
    CREDENTIAL.adopt("sbt_1", "sbt_1b", later(1))
    monkeypatch.setenv("COMPUTER_AGENT_EGRESS_PROXY_SECRET", "sbt_2")

    CREDENTIAL.adopt("sbt_1b", "sbt_1c", later(2))

    settings = settings_from_env()
    assert settings.api_key() == "sbt_2"
    import os

    assert os.environ["COMPUTER_AGENT_EGRESS_PROXY_SECRET"] == "sbt_2"


def settings_from_env(**kw):
    import os

    params = dict(
        agent_id="a",
        engine="gitagent",
        base_url=EGRESS,
        api_key=None,
        llm_url=f"{EGRESS}/llm",
        mcp_url=f"{EGRESS}/mcp",
        guardrails_url=f"{EGRESS}/guardrails",
        telemetry_url=None,
        gap_dir=None,
    )
    params.update(kw)
    return resolve_settings(env=os.environ, **params)


def test_explicit_key_follows_its_chain(monkeypatch):
    monkeypatch.setenv("COMPUTER_AGENT_EGRESS_PROXY_SECRET", "sbt_env")
    CREDENTIAL.configure([f"{EGRESS}/llm"])
    settings = settings_from_env(api_key="sbt_k")

    CREDENTIAL.adopt("sbt_k", "sbt_k2", later())

    assert settings.api_key() == "sbt_k2"
    import os

    assert os.environ["COMPUTER_AGENT_EGRESS_PROXY_SECRET"] == "sbt_env"


def test_concurrent_adopt():
    """Many threads racing two successors of one token: exactly one wins and
    the chain stays whole."""
    c = cred()
    results: list[bool] = []
    barrier = threading.Barrier(50)

    def run(i: int) -> None:
        barrier.wait()
        token = "sbt_b" if i % 2 else "sbt_c"
        results.append(c.adopt("sbt_a", token, later(4 + i % 2)))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1
    chain = c._chains["sbt_a"]
    assert len(chain.tokens) == 2
    assert c.resolve("sbt_a") == chain.tokens[-1] in ("sbt_b", "sbt_c")


def test_never_logs_tokens(caplog):
    c = cred()

    with caplog.at_level("DEBUG", logger="computeragent.harness.egress_token"):
        c.adopt("sbt_a", "sbt_secret_b", later())
        c.adopt("sbt_a", "sbt_secret_x", later(9))  # superseded
        c.adopt("sbt_z", "nope", later())

    text = " ".join(r.getMessage() for r in caplog.records)
    assert "sbt_" not in text
    infos = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(infos) == 1
    assert "reissued" in infos[0].getMessage()


# ── stamping ─────────────────────────────────────────────────────────────────


def test_request_hook_stamps_newest():
    c = cred()
    c.adopt("sbt_a", "sbt_b", later())

    headers = {"x-api-key": "sbt_a"}
    c.stamp(f"{EGRESS}/llm/v1/messages", headers)
    assert headers == {"x-api-key": "sbt_b"}

    headers = {"authorization": "Bearer sbt_a"}
    c.stamp(f"{EGRESS}/mcp/v1/jira", headers)
    assert headers == {"authorization": "Bearer sbt_b"}

    headers = {"x-api-key": "sbt_unknown"}
    c.stamp(f"{EGRESS}/x", headers)
    assert headers == {"x-api-key": "sbt_unknown"}

    headers = {}
    c.stamp(f"{EGRESS}/x", headers)
    assert headers == {}

    headers = {"x-api-key": "sbt_a"}
    c.stamp("http://elsewhere.test/x", headers)
    assert headers == {"x-api-key": "sbt_a"}


def test_a_reissue_from_another_origin_is_ignored():
    c = cred()

    adopted = c.observe(
        "http://elsewhere.test/x", "sbt_a", {HEADER_TOKEN: "sbt_b", HEADER_EXPIRES_AT: later()}
    )

    assert adopted is False
    assert c.resolve("sbt_a") == "sbt_a"


def reissue(token: str, hours: float = 4) -> httpx.Response:
    return httpx.Response(200, headers={HEADER_TOKEN: token, HEADER_EXPIRES_AT: later(hours)})


@respx.mock
async def test_hooks_on_clients():
    """A reissue seen by one client is carried by the next request of every other."""
    CREDENTIAL.configure([f"{EGRESS}/llm"])
    first = respx.get(f"{EGRESS}/a").mock(return_value=reissue("sbt_b"))
    second = respx.get(f"{EGRESS}/b").mock(return_value=httpx.Response(200))

    async with httpx.AsyncClient(event_hooks=egress_event_hooks(True)) as owned:
        await owned.get(f"{EGRESS}/a", headers={"x-api-key": "sbt_a"})

    injected = httpx.Client()
    install(install(injected))
    assert len(injected.event_hooks["request"]) == 1
    injected.get(f"{EGRESS}/b", headers={"x-api-key": "sbt_a"})
    injected.close()

    assert first.calls.last.request.headers["x-api-key"] == "sbt_a"
    assert second.calls.last.request.headers["x-api-key"] == "sbt_b"


@respx.mock
async def test_gitagent_clients_carry_the_newest_token():
    from computeragent.engines.git_agent import _build_anthropic_client, _build_openai_client

    CREDENTIAL.configure([f"{EGRESS}/llm"])
    CREDENTIAL.adopt("sbt_a", "sbt_b", later())
    route = respx.post(f"{EGRESS}/llm/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "m",
                "type": "message",
                "role": "assistant",
                "model": "x",
                "content": [],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
    )

    client = _build_anthropic_client(
        {"ANTHROPIC_API_KEY": "sbt_a", "ANTHROPIC_BASE_URL": f"{EGRESS}/llm"}
    )
    await client.messages.create(
        model="x", max_tokens=1, messages=[{"role": "user", "content": "hi"}]
    )

    assert route.calls.last.request.headers["x-api-key"] == "sbt_b"
    openai_client = _build_openai_client(
        {"OPENAI_API_KEY": "sbt_a", "GITCLAW_MODEL_BASE_URL": f"{EGRESS}/llm"}
    )
    assert any(
        getattr(fn, "_computeragent_egress", False)
        for fn in openai_client._client.event_hooks["request"]
    )


def test_stamping_session():
    pytest.importorskip("requests")
    import requests

    CREDENTIAL.configure([f"{EGRESS}/llm"])
    CREDENTIAL.adopt("sbt_a", "sbt_b", later())
    session = stamping_session()
    seen = {}

    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            seen["key"] = request.headers.get("x-api-key")
            resp = requests.Response()
            resp.status_code = 200
            resp.headers.update({HEADER_TOKEN: "sbt_c", HEADER_EXPIRES_AT: later(8)})
            resp.request = request
            return resp

        def close(self):
            pass

    session.mount(EGRESS, Adapter())
    session.post(f"{EGRESS}/otlp/v1/traces", headers={"x-api-key": "sbt_a"}, data=b"x")

    assert seen["key"] == "sbt_b"
    assert CREDENTIAL.resolve("sbt_a") == "sbt_c"
