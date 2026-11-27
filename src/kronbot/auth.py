"""Примитивы авторизации ITMO.ID."""

import base64
import asyncio
import hashlib
import html
import json
import re
import secrets
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Awaitable, Callable
from urllib.parse import urljoin

import httpx
from urllib.parse import parse_qs, urlencode, urlsplit

ISSUER = "https://id.itmo.ru/auth/realms/itmo"
CLIENT_ID = "student-personal-cabinet"
CALLBACK = "https://my.itmo.ru/login/callback"


class InvalidCallback(ValueError):
    """Отклонён недостоверный callback."""


def authorization_request() -> tuple[str, str, str]:
    """Вернуть URL, state и verifier отдельной попытки PKCE."""
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    query = urlencode({"response_type": "code", "client_id": CLIENT_ID, "redirect_uri": CALLBACK,
                       "scope": "openid profile", "state": state, "code_challenge_method": "S256",
                       "code_challenge": challenge})
    return f"{ISSUER}/protocol/openid-connect/auth?{query}", state, verifier


def callback_code(url: str, state: str) -> str:
    """Принять только официальный callback с однозначными параметрами."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "my.itmo.ru"
                or parsed.path != "/login/callback" or parsed.fragment):
            raise InvalidCallback("Недопустимый адрес callback")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (len(query.get("code", [])) != 1 or not query["code"][0]
                or query.get("state") != [state] or "error" in query
                or len(query.get("iss", [])) > 1
                or ("iss" in query and query["iss"] != [ISSUER])):
            raise InvalidCallback("Недопустимые параметры callback")
        return query["code"][0]
    except ValueError as exc:
        raise InvalidCallback("Недопустимый callback") from exc


@dataclass(frozen=True)
class Session:
    sub: str
    access_token: str
    access_expires_at: int
    refresh_token: str | None
    refresh_expires_at: int | None
    isu: str | None = None


def parse_isu(value: object) -> str | None:
    """Принять только положительный десятичный номер из доверенного ответа."""
    if type(value) is int and value > 0:
        return str(value)
    if (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and any(c != "0" for c in value)
    ):
        return value
    return None


class AuthError(Exception):
    """Ошибка входа, безопасная для вывода."""


class LoginRequired(AuthError):
    """Сессия больше не действительна."""


class Restricted(AuthError):
    def __init__(self, status: int, retry_after: str | None = None):
        super().__init__("Доступ ограничен или ответ изменился. Проверьте условия доступа в ИТМО.")
        self.status = status
        self.retry_after = retry_after


LOGIN_FAILED = "ITMO.ID не завершил вход. Проверьте данные или войдите через браузер."
TOKEN_URL = ISSUER + "/protocol/openid-connect/token"
USERINFO_URL = ISSUER + "/protocol/openid-connect/userinfo"


class _LoginForm(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.action = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form" and attributes.get("id") == "kc-form-login":
            self.action = attributes.get("action")


def login_action(document: str, base: str) -> str:
    match = re.search(r'"loginAction"\s*:\s*("(?:[^"\\]|\\.)*")', document)
    try:
        if match:
            action = html.unescape(json.loads(match.group(1)))
        else:
            parser = _LoginForm()
            parser.feed(document)
            action = parser.action
        if not isinstance(action, str) or not action:
            raise ValueError()
        resolved = urljoin(base, action)
        parsed = urlsplit(resolved)
        if (parsed.scheme != "https" or parsed.netloc != "id.itmo.ru"
                or parsed.path != "/auth/realms/itmo/login-actions/authenticate"
                or parsed.fragment):
            raise ValueError()
        return resolved
    except (ValueError, TypeError):
        raise AuthError(LOGIN_FAILED) from None


def _access_exp(token: str, now: float) -> int:
    try:
        parts = token.split(".")
        if len(parts) != 3 or not all(parts):
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        exp = data["exp"]
        if (not isinstance(exp, int) or isinstance(exp, bool) or exp <= now
                or data["iss"] != ISSUER or data["azp"] != CLIENT_ID):
            raise ValueError()
        return exp
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise AuthError("Недействительный access-токен My ITMO.") from None


class ItmoAuth:
    """Изолированный клиент ITMO.ID; cookie не покидают попытку входа."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None,
                 on_restricted: Callable[[Restricted], Awaitable[None]] | None = None,
                 now: Callable[[], float] = time.time):
        self.transport = transport
        self.on_restricted = on_restricted
        self.now = now

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport, follow_redirects=False, trust_env=False,
                                 timeout=15, headers={"User-Agent": "kronbot/0.1.0"})

    async def _request(self, client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            async with asyncio.timeout(15):
                response = await client.request(method, url, **kwargs)
        except (httpx.HTTPError, TimeoutError):
            raise AuthError("Временная ошибка связи с ITMO.ID.") from None
        if response.status_code in (403, 429):
            error = Restricted(response.status_code, response.headers.get("Retry-After"))
            if self.on_restricted:
                await self.on_restricted(error)
            raise error
        return response

    async def _userinfo(self, client: httpx.AsyncClient, token: str) -> tuple[str, str | None]:
        response = await self._request(client, "GET", USERINFO_URL, headers={"Authorization": f"Bearer {token}"})
        try:
            if not response.is_success:
                raise ValueError()
            body = response.json()
            sub = body["sub"]
            if not isinstance(sub, str) or not sub.strip():
                raise ValueError()
            # Only an explicit claim from authenticated userinfo is usable.
            return sub, parse_isu(body.get("isu"))
        except (ValueError, KeyError, TypeError):
            raise AuthError("Не удалось подтвердить владельца токена.") from None

    async def _exchange(
        self, client: httpx.AsyncClient, data: dict[str, str], sub: str | None = None,
        isu: str | None = None,
    ) -> Session:
        response = await self._request(
            client, "POST", TOKEN_URL, data={"client_id": CLIENT_ID, **data}
        )
        received_at = int(self.now())
        try:
            body = response.json()
            if not response.is_success:
                if isinstance(body, dict) and body.get("error") == "invalid_grant":
                    raise LoginRequired("Нужен новый вход.")
                raise ValueError()
            access, refresh = body["access_token"], body["refresh_token"]
            expires, refresh_expires = body["expires_in"], body["refresh_expires_in"]
            if (not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh
                    or type(expires) is not int or expires <= 0
                    or type(refresh_expires) is not int or refresh_expires <= 0):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise AuthError("Не удалось обновить токены ITMO.ID.") from None
        if sub is None:
            sub, isu = await self._userinfo(client, access)
        return Session(
            sub, access, received_at + expires, refresh, received_at + refresh_expires, isu
        )

    async def _code(self, client: httpx.AsyncClient, callback: str, state: str, verifier: str) -> Session:
        try:
            code = callback_code(callback, state)
        except InvalidCallback:
            raise AuthError(LOGIN_FAILED) from None
        return await self._exchange(client, {"grant_type": "authorization_code", "redirect_uri": CALLBACK,
                                             "code": code, "code_verifier": verifier})

    async def login_password(self, username: str, password: str) -> Session:
        url, state, verifier = authorization_request()
        async with self._client() as client:
            response = await self._request(client, "GET", url)
            if response.status_code != 200:
                raise AuthError(LOGIN_FAILED)
            action = login_action(response.text, url)
            response = await self._request(client, "POST", action,
                                           data={"username": username, "password": password, "rememberMe": "on"})
            if response.status_code != 302:
                raise AuthError(LOGIN_FAILED)
            return await self._code(client, response.headers.get("Location", ""), state, verifier)

    async def import_access(self, token: str) -> Session:
        expiry = _access_exp(token, self.now())
        async with self._client() as client:
            sub, isu = await self._userinfo(client, token)
            return Session(sub, token, expiry, None, None, isu)

    async def import_refresh(self, token: str) -> Session:
        async with self._client() as client:
            return await self._exchange(client, {"grant_type": "refresh_token", "refresh_token": token})

    async def refresh(self, session: Session) -> Session:
        if not session.refresh_token or not session.refresh_expires_at or session.refresh_expires_at <= self.now():
            raise LoginRequired("Нужен новый вход.")
        async with self._client() as client:
            return await self._exchange(
                client,
                {"grant_type": "refresh_token", "refresh_token": session.refresh_token},
                session.sub,
                session.isu,
            )

    async def login_browser(self, cancel: asyncio.Event) -> Session:
        from .browser import BrowserRestricted, browser_login
        url, state, verifier = authorization_request()
        try:
            callback = await browser_login(url, CALLBACK, cancel)
        except BrowserRestricted as exc:
            error = Restricted(exc.status, exc.retry_after)
            if self.on_restricted:
                await self.on_restricted(error)
            raise error from None
        if cancel.is_set():
            raise AuthError("Вход отменён.")
        async with self._client() as client:
            return await self._code(client, callback, state, verifier)
