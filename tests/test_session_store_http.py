"""The HTTP session store.

``load`` must never raise (the engine awaits it at session start), and the
only credential is ``x-api-key``: the newest sandbox token, which the egress
proxy swaps for the Lyzr key.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from computeragent.harness.egress_token import CREDENTIAL
from computeragent.session_stores import resolve_session_store
from computeragent.session_stores.http import (
    HttpSessionStore,
    SessionStoreAppendError,
    http_session_store_builder,
    with_base_url,
)

BASE = "https://cas.example"
ENTRIES_URL = f"{BASE}/session-store/entries"
KEY = {"project_key": "proj", "session_id": "sess-1"}


def store(**kw) -> HttpSessionStore:
    params = {"base_url": BASE}
    params.update(kw)
    return HttpSessionStore(**params)


# ── construction ─────────────────────────────────────────────────────────────


def test_base_url_is_required():
    with pytest.raises(ValueError, match="base_url"):
        HttpSessionStore(base_url="")


def test_builder_requires_only_a_base_url():
    build = http_session_store_builder()
    with pytest.raises(ValueError, match="base_url"):
        build({})
    with pytest.raises(ValueError, match="base_url"):
        build({"baseUrl": BASE})  # the camel-case key is not read
    assert isinstance(build({"base_url": BASE, "token": "dropped"}), HttpSessionStore)


# ── load ─────────────────────────────────────────────────────────────────────


@respx.mock
async def test_load_returns_entries():
    respx.get(ENTRIES_URL).mock(
        return_value=httpx.Response(200, json={"entries": [{"type": "user", "uuid": "a"}]})
    )

    assert await store().load(KEY) == [{"type": "user", "uuid": "a"}]


@respx.mock
async def test_load_sends_the_key_as_query_params():
    route = respx.get(ENTRIES_URL).mock(return_value=httpx.Response(200, json={"entries": []}))

    await store().load(KEY)

    url = route.calls.last.request.url
    assert url.params["sessionId"] == "sess-1"
    assert url.params["projectKey"] == "proj"


@respx.mock
async def test_load_404_is_none():
    respx.get(ENTRIES_URL).mock(return_value=httpx.Response(404))

    assert await store().load(KEY) is None


@respx.mock
async def test_load_of_an_empty_list_is_none():
    respx.get(ENTRIES_URL).mock(return_value=httpx.Response(200, json={"entries": []}))

    assert await store().load(KEY) is None


@respx.mock
@pytest.mark.parametrize(
    ("label", "response"),
    [
        ("unauthorized", httpx.Response(401)),
        ("server error", httpx.Response(500)),
        ("malformed json", httpx.Response(200, text="not json")),
        ("timeout", httpx.TimeoutException("slow")),
        ("connection refused", httpx.ConnectError("no route")),
    ],
)
async def test_load_never_raises(label, response, caplog):
    """A store outage must cost this session its RESUME, not its life. The
    warning is the only signal an operator gets that an agent started with no
    memory, so it is part of the contract."""
    if isinstance(response, Exception):
        respx.get(ENTRIES_URL).mock(side_effect=response)
    else:
        respx.get(ENTRIES_URL).mock(return_value=response)

    with caplog.at_level("WARNING"):
        assert await store().load(KEY) is None

    assert any("load failed" in r.message for r in caplog.records), label


# ── append ───────────────────────────────────────────────────────────────────


@respx.mock
async def test_append_posts_the_batch():
    route = respx.post(ENTRIES_URL).mock(return_value=httpx.Response(200, json={"appended": 1}))

    await store().append(KEY, [{"type": "user", "uuid": "a"}])

    body = json.loads(route.calls.last.request.content)
    assert body["sessionId"] == "sess-1"
    assert body["projectKey"] == "proj"
    assert body["entries"] == [{"type": "user", "uuid": "a"}]


@respx.mock
async def test_empty_append_makes_no_request():
    route = respx.post(ENTRIES_URL).mock(return_value=httpx.Response(200, json={}))

    await store().append(KEY, [])

    assert route.call_count == 0


@respx.mock
async def test_4xx_raises_after_exactly_one_request():
    """A 4xx says the request itself is wrong. The SDK already retries three
    times; retrying here too would mean nine attempts for nothing."""
    route = respx.post(ENTRIES_URL).mock(return_value=httpx.Response(413))

    with pytest.raises(SessionStoreAppendError):
        await store().append(KEY, [{"type": "user"}])

    assert route.call_count == 1


@respx.mock
async def test_5xx_is_retried_exactly_once():
    route = respx.post(ENTRIES_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(SessionStoreAppendError):
        await store().append(KEY, [{"type": "user"}])

    assert route.call_count == 2


@respx.mock
async def test_a_transient_failure_that_then_succeeds_is_not_an_error():
    route = respx.post(ENTRIES_URL)
    route.side_effect = [httpx.ConnectError("blip"), httpx.Response(200, json={})]

    await store().append(KEY, [{"type": "user"}])

    assert route.call_count == 2


@respx.mock
async def test_truncation_is_reported(caplog):
    """The server shortened an entry rather than rejecting the batch — better
    than a hole in the transcript, but it changes what the model sees."""
    respx.post(ENTRIES_URL).mock(return_value=httpx.Response(200, json={"truncated": 2}))

    with caplog.at_level("WARNING"):
        await store().append(KEY, [{"type": "user"}])

    assert any("truncated" in r.message for r in caplog.records)


@respx.mock
async def test_appends_are_serialised_in_call_order():
    """The contract makes ordering ours to keep, and the server's dedup is a
    read-then-write that two in-flight appends would race."""
    seen: list[str] = []

    async def record(request: httpx.Request) -> httpx.Response:
        uuid = json.loads(request.content)["entries"][0]["uuid"]
        await asyncio.sleep(0.02 if uuid == "first" else 0)
        seen.append(uuid)
        return httpx.Response(200, json={})

    respx.post(ENTRIES_URL).mock(side_effect=record)
    s = store()

    await asyncio.gather(
        s.append(KEY, [{"type": "user", "uuid": "first"}]),
        s.append(KEY, [{"type": "user", "uuid": "second"}]),
    )

    assert seen == ["first", "second"]


# ── the credential ───────────────────────────────────────────────────────────


def later(hours: float = 4) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


@respx.mock
async def test_sends_only_the_api_key(monkeypatch):
    monkeypatch.setenv("COMPUTER_AGENT_EGRESS_PROXY_SECRET", "sbt_a")
    CREDENTIAL.configure([BASE])
    load = respx.get(ENTRIES_URL).mock(return_value=httpx.Response(200, json={"entries": []}))
    append = respx.post(ENTRIES_URL).mock(return_value=httpx.Response(200, json={}))
    refresh = respx.post(f"{BASE}/v1/auth/token").mock(return_value=httpx.Response(200))
    s = store()

    await s.load(KEY)
    await s.append(KEY, [{"type": "user"}])
    for route in (load, append):
        headers = route.calls.last.request.headers
        assert headers["x-api-key"] == "sbt_a"
        assert "x-ca-session-store-token" not in headers
        assert "authorization" not in headers

    CREDENTIAL.adopt("sbt_a", "sbt_b", later())
    await s.load(KEY)
    assert load.calls.last.request.headers["x-api-key"] == "sbt_b"
    assert not refresh.called
    await s.close()


@respx.mock
async def test_no_key_means_no_credential(monkeypatch):
    monkeypatch.delenv("COMPUTER_AGENT_EGRESS_PROXY_SECRET", raising=False)
    route = respx.get(ENTRIES_URL).mock(return_value=httpx.Response(200, json={"entries": []}))

    await store().load(KEY)

    assert "x-api-key" not in route.calls.last.request.headers


@respx.mock
async def test_proxy_401_is_not_a_store_error(caplog, monkeypatch):
    monkeypatch.setenv("COMPUTER_AGENT_EGRESS_PROXY_SECRET", "sbt_a")
    respx.get(ENTRIES_URL).mock(
        return_value=httpx.Response(
            401, json={"error": {"code": "TOKEN_INVALID", "message": "leak"}}
        )
    )

    with caplog.at_level("WARNING", logger="computeragent.session_stores.http"):
        assert await store().load(KEY) is None

    text = " ".join(r.getMessage() for r in caplog.records)
    assert "HTTP 401" in text
    assert "leak" not in text


async def test_close_is_idempotent():
    s = store()
    await s.close()
    await s.close()


# ── COMPUTER_AGENT_EGRESS_SESSION_STORE_URL ──────────────────────────────────────────

EGRESS_STORE = "http://egress/computer-agent-api"


@respx.mock
async def test_session_store_url_precedence(caplog, monkeypatch):
    monkeypatch.setenv("COMPUTER_AGENT_EGRESS_PROXY_SECRET", "sbt_a")
    route = respx.get(f"{EGRESS_STORE}/session-store/entries").mock(
        return_value=httpx.Response(200, json={"entries": []})
    )

    with caplog.at_level("INFO", logger="computeragent.session_stores.http"):
        spec = with_base_url(
            {"kind": "http", "options": {"baseUrl": "http://cas:8786", "token": "t"}}, EGRESS_STORE
        )
    assert spec == {"kind": "http", "options": {"base_url": EGRESS_STORE}}
    await resolve_session_store(spec).load(KEY)
    assert route.called
    (info,) = [r for r in caplog.records if r.levelname == "INFO"]
    assert "http" not in info.getMessage().replace("COMPUTER_AGENT_EGRESS_SESSION_STORE_URL", "")

    spec = with_base_url({"kind": "http", "options": {"base_url": "http://other"}}, EGRESS_STORE)
    assert spec["options"]["base_url"] == EGRESS_STORE

    assert with_base_url("http", EGRESS_STORE) == {
        "kind": "http",
        "options": {"base_url": EGRESS_STORE},
    }

    unset = {"kind": "http", "options": {"base_url": BASE}}
    assert with_base_url(unset, None) is unset
    with pytest.raises(ValueError, match="base_url is required"):
        resolve_session_store(with_base_url({"kind": "http", "options": {"baseUrl": BASE}}, None))

    assert with_base_url("memory", EGRESS_STORE) == "memory"
    instance = store()
    assert with_base_url(instance, EGRESS_STORE) is instance


# ── deliberate omissions ─────────────────────────────────────────────────────


@pytest.mark.parametrize("method", ["list_sessions", "delete", "list_subkeys"])
def test_optional_methods_are_absent_not_stubbed(method):
    """The SDK probes for these. `list_sessions` is project-scoped and would
    hand one sandbox a listing of every other session, so a well-meaning stub
    would change behaviour rather than preserve it."""
    assert not hasattr(store(), method)
