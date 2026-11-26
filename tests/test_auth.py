from urllib.parse import urlencode
from urllib.parse import parse_qs, urlsplit

import httpx

import pytest

from kronbot.auth import CALLBACK, ISSUER, InvalidCallback, authorization_request, callback_code


def test_pkce_attempts_and_callback():
    url, state, verifier = authorization_request()
    assert url.startswith(ISSUER + "/protocol/openid-connect/auth?")
    assert state != verifier
    assert callback_code(CALLBACK + "?" + urlencode({"state": state, "code": "ok", "iss": ISSUER}), state) == "ok"


@pytest.mark.parametrize("suffix", [
    "?code=x&state=s&state=s", "?code=x&state=s&error=access_denied",
    "?code=x&state=s&iss=bad", "?code=x&state=s#fragment",
])
def test_reject_bad_callback_parameters(suffix):
    with pytest.raises(InvalidCallback):
        callback_code(CALLBACK + suffix, "s")


@pytest.mark.parametrize("url", [
    "http://my.itmo.ru/login/callback?code=x&state=s",
    "https://my.itmo.ru:443/login/callback?code=x&state=s",
    "https://user@my.itmo.ru/login/callback?code=x&state=s",
    "https://my.itmo.ru.evil/login/callback?code=x&state=s",
])
def test_reject_bad_callback_origins(url):
    with pytest.raises(InvalidCallback):
        callback_code(url, "s")


@pytest.mark.parametrize("form", [
    '{"loginAction": "/auth/realms/itmo/login-actions/authenticate?x=1\\u0026y=2"}',
    '<form id="kc-form-login" action="/auth/realms/itmo/login-actions/authenticate?x=1&amp;y=2"></form>',
])
async def test_password_flow(form):
    from kronbot.auth import ItmoAuth
    calls = []
    state = None

    def handler(request):
        nonlocal state
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/auth"):
            state = parse_qs(urlsplit(str(request.url)).query)["state"][0]
            return httpx.Response(200, text=form)
        if request.url.path.endswith("/authenticate"):
            assert b"password=secret" in request.content
            return httpx.Response(302, headers={"Location": CALLBACK + "?" + urlencode({"code": "abc", "state": state})})
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "access", "refresh_token": "refresh", "expires_in": 100, "refresh_expires_in": 200})
        return httpx.Response(200, json={"sub": "owner"})

    session = await ItmoAuth(transport=httpx.MockTransport(handler)).login_password("user", "secret")
    assert session.sub == "owner"
    assert session.refresh_token == "refresh"
    assert [method for method, _ in calls] == ["GET", "POST", "POST", "GET"]


async def test_password_never_sent_to_external_action():
    from kronbot.auth import AuthError, ItmoAuth
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, text='{\"loginAction\":\"https://evil.test/auth/realms/itmo/login-actions/authenticate\"}')
    with pytest.raises(AuthError):
        await ItmoAuth(transport=httpx.MockTransport(handler)).login_password("user", "secret")
    assert len(calls) == 1


async def test_refresh_import_confirms_owner():
    from kronbot.auth import ItmoAuth
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "POST":
            assert b"refresh_token=old" in request.content
            return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 100, "refresh_expires_in": 200})
        assert request.headers["authorization"] == "Bearer new-access"
        return httpx.Response(200, json={"sub": "owner"})
    session = await ItmoAuth(transport=httpx.MockTransport(handler)).import_refresh("old")
    assert session.sub == "owner" and session.refresh_token == "new-refresh"
    assert len(calls) == 2
