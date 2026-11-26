"""Ограниченный клиент спортивного API My ITMO."""

import asyncio
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from .auth import ItmoAuth, LoginRequired, Restricted, Session

BASE = "https://my.itmo.ru/api/sport/"


class SportError(Exception):
    pass


class LocalLimit(SportError):
    def __init__(self, until: int):
        self.until = until
        super().__init__(f"Локальное ограничение до {datetime.fromtimestamp(until, timezone.utc).isoformat()}.")

class NetworkBlocked(LocalLimit):
    """Действует сохранённый запрет после ответа сервера."""



class ApiRefusal(SportError):
    def __init__(self, code: int, no_places: bool = False):
        self.code = code
        self.no_places = no_places
        super().__init__(f"Отказ API: {code}.")


class HttpUnauthorized(SportError):
    pass


@dataclass(frozen=True)
class Lesson:
    id: int
    section_name: str
    room_name: str
    starts_at: datetime
    section_level: int
    type_id: int
    available: int
    signed: bool
    intersection: bool
    can_sign_in: bool


@dataclass(frozen=True)
class Enrollment:
    status: str
    refusal_code: int | None = None


def clean_text(value: str) -> str:
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())


def _positive_id(value):
    return type(value) is int and value > 0


def _days(result):
    if not isinstance(result, list):
        raise SportError("Некорректный ответ спортивного API.")
    for day in result:
        if not isinstance(day, dict):
            raise SportError("Некорректный ответ спортивного API.")
        lessons = day.get("lessons")
        if lessons is None:
            continue
        if not isinstance(lessons, list):
            raise SportError("Некорректный ответ спортивного API.")
        yield from lessons


def _lesson(raw):
    try:
        if not isinstance(raw, dict):
            return None
        ident = raw["id"]
        level, typ, available = raw["section_level"], raw["type_id"], raw["available"]
        signed, permitted = raw["signed"], raw["can_sign_in"]["can_sign_in"]
        starts = datetime.fromisoformat(raw["date"])
        if (not _positive_id(ident) or type(level) is not int or type(typ) is not int
                or type(available) is not int or available < 0 or type(signed) is not bool
                or type(permitted) is not bool or starts.utcoffset() is None):
            return None
        section, room = raw.get("section_name", ""), raw.get("room_name", "")
        intersection = raw.get("intersection", False)
        if not isinstance(section, str) or not isinstance(room, str) or type(intersection) is not bool:
            return None
        return Lesson(ident, clean_text(section), clean_text(room), starts, level, typ, available, signed, intersection, permitted)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


class SportClient:
    def __init__(self, store, *, transport=None, auth_transport=None, now=time.time):
        self.store, self.now = store, now
        self.client = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False, timeout=15,
                                        headers={"User-Agent": "kronbot/0.1.0", "Accept-Language": "ru", "Accept": "application/json"})
        self.auth = ItmoAuth(transport=auth_transport, on_restricted=self._auth_restricted, now=now)
        self.pending_session = None
        self.stopped = False
        self.in_flight = None

    async def close(self):
        await self.client.aclose()

    def check_blocks(self):
        now = self.now()
        blocks = self.store.data["blocked_until"]
        until = max(blocks.values(), default=0)
        if until > now:
            raise NetworkBlocked(until)
        if self.stopped:
            raise SportError("Работа остановлена.")

    def _block(self, host, status, retry_after=None):
        now = int(self.now())
        until = now + (120 if status == 429 else 86400)
        if status == 429 and retry_after:
            try:
                if retry_after.isdecimal():
                    until = max(until, now + int(retry_after))
                else:
                    parsed = parsedate_to_datetime(retry_after)
                    until = max(until, int(parsed.timestamp()))
            except (ValueError, TypeError, OverflowError):
                pass
        self.store.data["blocked_until"][host] = max(until, self.store.data["blocked_until"].get(host, 0))
        self.store.save()
        raise Restricted(status, retry_after)

    async def _auth_restricted(self, error):
        self._block("id.itmo.ru", error.status, error.retry_after)

    def _session(self):
        value = self.store.data["session"]
        if value is None:
            raise LoginRequired("Нужен новый вход.")
        return Session(**value)

    async def refresh(self):
        self.check_blocks()
        if self.pending_session is None:
            try:
                self.pending_session = await self.auth.refresh(self._session())
            except LoginRequired:
                self.store.data["session"] = None
                self.store.save()
                raise
        self.store.data["session"] = asdict(self.pending_session)
        self.store.save()
        self.pending_session = None

    async def _ensure_session(self):
        if self.pending_session is not None:
            self.store.data["session"] = asdict(self.pending_session)
            self.store.save()
            self.pending_session = None
        session = self._session()
        if session.refresh_token and session.access_expires_at <= self.now() + 30:
            await self.refresh()
            session = self._session()
        elif session.access_expires_at <= self.now():
            self.store.data["session"] = None
            self.store.save()
            raise LoginRequired("Нужен новый вход.")
        return session

    def reserve(self, *, enrollment=None):
        self.check_blocks()
        now = int(self.now())
        times = [t for t in self.store.data["sport_request_times"] if t > now - 3600]
        if len(times) >= 60:
            raise LocalLimit(min(times) + 3600)
        last = self.store.data["last_enroll_at"]
        if enrollment is not None and last is not None and last + 120 > now:
            raise LocalLimit(last + 120)
        times.append(now)
        self.store.data["sport_request_times"] = times
        if enrollment is not None:
            enrollment["status"] = "uncertain"
            self.store.data["last_enroll_at"] = now
        self.store.save()

    async def _request(self, method, path, *, params=None, json=None, enrollment=None, before_post=None):
        for attempt in range(2):
            self.check_blocks()
            session = await self._ensure_session()
            if before_post is not None and not before_post():
                raise SportError("Запись остановлена.")
            self.reserve(enrollment=enrollment)
            if enrollment is not None:
                self.in_flight = enrollment["lesson_id"]
            try:
                async with asyncio.timeout(15):
                    response = await self.client.request(method, BASE + path, params=params, json=json,
                                                         headers={"Authorization": f"Bearer {session.access_token}"})
            except (httpx.HTTPError, TimeoutError):
                raise SportError("Временная ошибка спортивного API.") from None
            finally:
                self.in_flight = None
            if response.status_code in (403, 429):
                self._block("my.itmo.ru", response.status_code, response.headers.get("Retry-After"))
            if response.status_code == 401:
                if method == "POST":
                    raise HttpUnauthorized("Нужен новый вход.")
                if attempt == 0:
                    await self.refresh()
                    continue
                self.store.data["session"] = None
                self.store.save()
                raise LoginRequired("Нужен новый вход.")
            if not response.is_success:
                raise SportError("Временная ошибка спортивного API.")
            if "text/html" in response.headers.get("content-type", "").lower():
                self._block("my.itmo.ru", 403)
            try:
                body = response.json()
                if not isinstance(body, dict) or "error_code" not in body:
                    raise ValueError()
                code = body["error_code"]
                if code is not None and type(code) is not int:
                    raise ValueError()
                if code not in (None, 0):
                    message = body.get("error_message")
                    no_places = isinstance(message, str) and "нельзя записать студента: нет свободных мест на занятии" in message.lower()
                    raise ApiRefusal(code, no_places)
                return body.get("result")
            except (ValueError, TypeError):
                raise SportError("Некорректный ответ спортивного API.") from None
        raise SportError("Нужен новый вход.")

    async def schedule(self, start: date, end: date) -> list[Lesson]:
        result = await self._request("GET", "sign/schedule", params={"date_start": start.isoformat(), "date_end": end.isoformat()})
        lessons = [_lesson(raw) for raw in _days(result)]
        lessons = [lesson for lesson in lessons if lesson is not None]
        counts = {}
        for lesson in lessons:
            counts[lesson.id] = counts.get(lesson.id, 0) + 1
        return sorted((lesson for lesson in lessons if counts[lesson.id] == 1), key=lambda x: (x.starts_at, x.id))

    async def calendar_ids(self, start: date, end: date) -> set[int]:
        result = await self._request("GET", "personal/calendar", params={"date_start": start.isoformat(), "date_end": end.isoformat()})
        ids = set()
        for item in _days(result):
            if not isinstance(item, dict) or not _positive_id(item.get("id")):
                raise SportError("Некорректный календарь спортивного API.")
            ids.add(item["id"])
        return ids

    async def can_spend_attempt(self, type_id: int) -> bool:
        result = await self._request("GET", "personal/debt" if type_id == 5 else "personal/have_attempts")
        if not isinstance(result, dict):
            raise SportError("Некорректный ответ о попытках.")
        free = result.get("free_attempts")
        return type(free) is int and free > 0 and (type_id == 5 or result.get("can_sign_in") is True)

    async def enroll(self, lesson_id: int, enrollment=None, before_post=None) -> Enrollment:
        if enrollment is None or before_post is None:
            raise SportError("Запись требует подтверждённой очереди.")
        try:
            result = await self._request("POST", "sign/schedule/lessons", json=[lesson_id], enrollment=enrollment, before_post=before_post)
            if isinstance(result, list) and all(type(x) is int for x in result) and lesson_id in result:
                return Enrollment("booked")
            return Enrollment("uncertain")
        except ApiRefusal as exc:
            if exc.code == 135 or exc.no_places:
                return Enrollment("waiting")
            if exc.code in (401, 403, 404):
                return Enrollment("uncertain")
            return Enrollment("rejected", exc.code)
