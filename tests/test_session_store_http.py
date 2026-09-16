"""The HTTP session store.

Two behaviours here are contractual rather than incidental, and both have a
failure mode that is silent if you get them wrong: ``load`` must never raise
(the engine awaits it at session start), and the capability token must never
travel on a header that the co-hosted translator and MCP bridge forward to a
third party.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx
import pytest
import respx

from computeragent.session_stores.http import (
    TOKEN_HEADER,
    HttpSessionStore,
    SessionStoreAppendError,
    expiry_of,
    http_session_store_builder,
)

BASE = "https://cas.example"
ENTRIES_URL = f"{BASE}/session-store/entries"
KEY = {"project_key": "proj", "session_id": "sess-1"}


def store(**kw) -> HttpSessionStore:
    params = {"base_url": BASE, "token": "opaque-token", "auto_refresh": False}
    params.update(kw)
    return HttpSessionStore(**params)


def jwt_expiring_in(seconds: int) -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + seconds}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"header.{payload}.signature"


# ── construction ─────────────────────────────────────────────────────────────


def test_base_url_is_required():
    with pytest.raises(ValueError, match="base_url"):
        HttpSessionStore(base_url="", token="t")


def test_token_is_required():
    with pytest.raises(ValueError, match="token"):
        HttpSessionStore(base_url=BASE, token="")


def test_builder_refuses_without_per_session_options():
    """Unlike mongo, this builder holds no defaults — the server sends them."""
    build = http_session_store_builder()
    with pytest.raises(ValueError, match="base_url"):
        build({})
    with pytest.raises(ValueError, match="token"):
        build({"base_url": BASE})


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


# ── the token header ─────────────────────────────────────────────────────────


@respx.mock
async def test_token_travels_on_its_own_header_only():
    """Structural, not stylistic: the same listener hosts the Anthropic-OpenAI
    translator and the MCP bridge, and both forward `authorization` /
    `x-api-key` verbatim to a third party."""
    route = respx.get(ENTRIES_URL).mock(return_value=httpx.Response(200, json={"entries": []}))

    await store().load(KEY)

    headers = route.calls.last.request.headers
    assert headers[TOKEN_HEADER] == "opaque-token"
    assert "authorization" not in headers
    assert "x-api-key" not in headers


# ── token refresh ────────────────────────────────────────────────────────────


def test_expiry_of_a_valid_jwt():
    assert expiry_of(jwt_expiring_in(3600)) is not None


@pytest.mark.parametrize("token", ["opaque", "a.b", "a.!!!.c", "", "a..c"])
def test_expiry_of_an_unreadable_token_is_none(token):
    """No hint means no refresh scheduled — harmless, because the server mints
    a fresh token at the next dispatch anyway."""
    assert expiry_of(token) is None


@respx.mock
async def test_refresh_swaps_the_token():
    respx.post(f"{BASE}/v1/auth/token").mock(
        return_value=httpx.Response(200, json={"token": "fresher", "expiresIn": 3600})
    )
    s = store()

    assert await s.refresh_token() is True
    assert s.token == "fresher"


@respx.mock
async def test_a_failed_refresh_keeps_the_old_token_and_does_not_raise():
    respx.post(f"{BASE}/v1/auth/token").mock(return_value=httpx.Response(500))
    s = store()

    assert await s.refresh_token() is False
    assert s.token == "opaque-token"


@respx.mock
async def test_close_cancels_a_pending_refresh():
    respx.get(ENTRIES_URL).mock(return_value=httpx.Response(200, json={"entries": []}))
    s = store(token=jwt_expiring_in(3600), auto_refresh=True)

    await s.load(KEY)  # arms the timer
    assert s._refresh_task is not None

    await s.close()

    assert s._refresh_task is None


async def test_close_is_safe_on_a_store_that_never_armed():
    await store().close()


# ── deliberate omissions ─────────────────────────────────────────────────────


@pytest.mark.parametrize("method", ["list_sessions", "delete", "list_subkeys"])
def test_optional_methods_are_absent_not_stubbed(method):
    """The SDK probes for these. `list_sessions` is project-scoped and would
    hand one sandbox a listing of every other session, so a well-meaning stub
    would change behaviour rather than preserve it."""
    assert not hasattr(store(), method)
