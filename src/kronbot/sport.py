"""Ограниченный клиент спортивного API My ITMO."""

import asyncio
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from .auth import ItmoAuth, LoginRequired, Restricted, Session, parse_isu

BASE = "https://my.itmo.ru/api/sport/"


class SportError(Exception):
    pass


class LocalLimit(SportError):
    def __init__(self, until: int):
        self.until = until
        super().__init__(f"Локальное ограничение до {datetime.fromtimestamp(until).astimezone().isoformat()}.")

class NetworkBlocked(LocalLimit):
    """Действует сохранённый запрет после ответа сервера."""
    def __init__(self, until: int):
        self.until = until
        SportError.__init__(self, f"Серверная блокировка до {datetime.fromtimestamp(until).astimezone().isoformat()}.")



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
    section_id: int | None = None
    lesson_level: int | None = None
    building_id: int | None = None
    room_id: int | None = None
    time_slot_id: int | None = None
    teacher_isu: int | None = None
    teacher_name: str = ""
    ends_at: datetime | None = None


def forecast_identity(lesson):
    """Ключ свободного посещения или занятия с нормативами."""
    if (not _positive_id(lesson.section_id) or type(lesson.section_level) is not int
            or lesson.section_level != 1 or type(lesson.type_id) is not int
            or lesson.type_id not in (2, 6) or not isinstance(lesson.starts_at, datetime)
            or lesson.starts_at.utcoffset() is None):
        return None
    return (lesson.section_id, lesson.section_level, 2, lesson.starts_at)


def forecast_prototypes(lessons, now):
    """Вернуть реальные прототипы будущих, ещё отсутствующих занятий."""
    identities = {identity for lesson in lessons if (identity := forecast_identity(lesson)) is not None}
    targets = set()
    result = []
    for lesson in sorted(lessons, key=lambda item: (item.starts_at, item.id)):
        identity = forecast_identity(lesson)
        if identity is None or lesson.section_level != 1:
            continue
        target = identity[:-1] + (identity[-1] + timedelta(days=14),)
        if target[-1].timestamp() <= now or target in identities or target in targets:
            continue
        targets.add(target)
        result.append(lesson)
    return result


@dataclass(frozen=True)
class Booking:
    id: int
    starts_at: datetime
    section_name: str
    room_name: str
    section_level: int
    lesson_group_id: int | None
    ends_at: datetime | None = None


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
        section = section if isinstance(section, str) else ""
        room = room if isinstance(room, str) else ""
        intersection = raw.get("intersection", False)
        intersection = intersection if type(intersection) is bool else False
        try:
            ends_at = datetime.fromisoformat(raw["date_end"])
            if ends_at.utcoffset() is None or ends_at <= starts:
                ends_at = None
        except (KeyError, TypeError, ValueError):
            ends_at = None
        return Lesson(
            ident,
            clean_text(section),
            clean_text(room),
            starts,
            level,
            typ,
            available,
            signed,
            intersection,
            permitted,
            section_id=raw.get("section_id") if _positive_id(raw.get("section_id")) else None,
            lesson_level=raw.get("lesson_level") if type(raw.get("lesson_level")) is int else None,
            building_id=raw.get("building_id") if type(raw.get("building_id")) is int else None,
            room_id=raw.get("room_id") if type(raw.get("room_id")) is int else None,
            time_slot_id=raw.get("time_slot_id") if _positive_id(raw.get("time_slot_id")) else None,
            teacher_isu=raw.get("teacher_isu") if _positive_id(raw.get("teacher_isu")) else None,
            teacher_name=clean_text(raw["teacher_fio"]) if isinstance(raw.get("teacher_fio"), str) else "",
            ends_at=ends_at,
        )
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
        self.forecast_invalid_rows = []
        self.opening_window_active = False

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

    async def account_isu(self) -> str | None:
        """Получить ИСУ текущего владельца из подтверждённого self endpoint."""
        self.check_blocks()
        session = self._session()
        # This optional lookup must not rotate a token after a successful login.
        if session.access_expires_at <= self.now():
            return None
        try:
            async with asyncio.timeout(15):
                response = await self.client.get(
                    "https://my.itmo.ru/api/eduPlanNew/programs",
                    headers={"Authorization": f"Bearer {session.access_token}"},
                )
        except (httpx.HTTPError, TimeoutError):
            raise SportError("Не удалось получить номер ИСУ.") from None
        if response.status_code in (403, 429):
            self._block("my.itmo.ru", response.status_code, response.headers.get("Retry-After"))
        if not response.is_success:
            raise SportError("Не удалось получить номер ИСУ.")
        if "text/html" in response.headers.get("content-type", "").lower():
            self._block("my.itmo.ru", 403)
        try:
            payload = response.json()
            if not isinstance(payload, dict) or "error_code" not in payload:
                raise ValueError()
            code = payload["error_code"]
            result = payload["result"]
            if ((code is not None and type(code) is not int) or code not in (None, 0)
                    or not isinstance(result, dict) or not isinstance(result.get("programs"), list)):
                raise ValueError()
            isu = parse_isu(result.get("isu"))
        except (TypeError, ValueError, KeyError):
            raise SportError("Не удалось получить номер ИСУ.") from None
        active = self.store.data["session"]
        if active is None or active["sub"] != session.sub:
            raise LoginRequired("Аккаунт изменился.")
        if isu is not None:
            old = active.get("isu")
            active["isu"] = isu
            try:
                self.store.save()
            except Exception:
                active["isu"] = old
                raise
        return isu

    def reserve(self, *, enrollment=None):
        self.check_blocks()
        now = int(self.now())
        times = [t for t in self.store.data["sport_request_times"] if t > now - 3600]
        if not (self.store.data["force_limits"] or self.opening_window_active) and len(times) >= 60:
            raise LocalLimit(sorted(times)[len(times) - 60] + 3600)
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
                if method != "GET":
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

    async def schedule(self, start: date, end: date, *, sport_type_id: int | None = None) -> list[Lesson]:
        params = {"date_start": start.isoformat(), "date_end": end.isoformat()}
        if sport_type_id is not None:
            if not _positive_id(sport_type_id):
                raise SportError("Некорректный вид спорта.")
            params["sport_type_id"] = str(sport_type_id)
        result = await self._request("GET", "sign/schedule", params=params)
        raw_lessons = list(_days(result))
        lessons = []
        invalid = []
        for raw in raw_lessons:
            lesson = _lesson(raw)
            if lesson is not None:
                lessons.append(lesson)
                if (lesson.section_level == 1 and lesson.type_id in (2, 6)
                        and forecast_identity(lesson) is None):
                    invalid.append(raw)
            else:
                invalid.append(raw)
        counts = {}
        for lesson in lessons:
            counts[lesson.id] = counts.get(lesson.id, 0) + 1
        # Keep the original key fields of invalid and duplicate rows. A row
        # blocks only forecasts it could actually compete with.
        invalid.extend(raw for raw in raw_lessons
                       if isinstance(raw, dict) and _positive_id(raw.get("id"))
                       and counts.get(raw["id"], 0) > 1)
        self.forecast_invalid_rows = invalid
        return sorted((lesson for lesson in lessons if counts[lesson.id] == 1), key=lambda x: (x.starts_at, x.id))

    async def sport_types(self) -> list[tuple[int, str]]:
        result = await self._request("GET", "sign/schedule/filters")
        if not isinstance(result, dict) or not isinstance(result.get("sport_type_id"), list):
            raise SportError("Не удалось получить виды спорта.")
        types = []
        seen = set()
        for item in result["sport_type_id"]:
            if not isinstance(item, dict) or not _positive_id(item.get("id")) or not isinstance(item.get("value"), str):
                raise SportError("Не удалось получить виды спорта.")
            ident, name = item["id"], clean_text(item["value"])
            if ident in seen or not name:
                raise SportError("Не удалось получить виды спорта.")
            seen.add(ident)
            types.append((ident, name))
        return types

    async def calendar_ids(self, start: date, end: date) -> set[int]:
        result = await self._request("GET", "personal/calendar", params={"date_start": start.isoformat(), "date_end": end.isoformat()})
        ids = set()
        for item in _days(result):
            if not isinstance(item, dict) or not _positive_id(item.get("id")):
                raise SportError("Некорректный календарь спортивного API.")
            ids.add(item["id"])
        return ids

    async def bookings(self, start: date, end: date) -> list[Booking]:
        result = await self._request(
            "GET",
            "personal/calendar",
            params={"date_start": start.isoformat(), "date_end": end.isoformat()},
        )
        bookings = []
        seen = set()
        for item in _days(result):
            try:
                if not isinstance(item, dict):
                    raise ValueError()
                ident, level = item["id"], item.get("section_level", 0)
                group = item.get("lesson_group_id")
                starts = datetime.fromisoformat(item["date"])
                ends = (
                    datetime.fromisoformat(item["date_end"])
                    if item.get("date_end") is not None
                    else None
                )
                section, room = item.get("section_name", ""), item.get("room_name")
                if room is None:
                    room = ""
                if (
                    not _positive_id(ident)
                    or ident in seen
                    or type(level) is not int
                    or (group is not None and not _positive_id(group))
                    or starts.utcoffset() is None
                    or (
                        ends is not None and (ends.utcoffset() is None or ends < starts)
                    )
                    or not isinstance(section, str)
                    or not isinstance(room, str)
                ):
                    raise ValueError()
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                raise SportError("Некорректный календарь спортивного API.") from None
            seen.add(ident)
            bookings.append(
                Booking(
                    ident,
                    starts,
                    clean_text(section),
                    clean_text(room),
                    level,
                    group,
                    ends,
                )
            )
        return sorted(bookings, key=lambda item: (item.starts_at, item.id))

    async def cancel_booking(self, booking: Booking, *, before_delete=None) -> None:
        if (
            not isinstance(booking, Booking)
            or not _positive_id(booking.id)
            or type(booking.section_level) is not int
            or booking.section_level not in (1, 2, 3, 4)
            or (
                booking.lesson_group_id is not None
                and not _positive_id(booking.lesson_group_id)
            )
            or (booking.section_level != 1 and booking.lesson_group_id is None)
            or not isinstance(booking.starts_at, datetime)
            or booking.starts_at.utcoffset() is None
            or booking.starts_at.timestamp() <= self.now()
            or before_delete is None
        ):
            raise SportError("Отмена этой записи невозможна.")
        if booking.section_level == 1:
            result = await self._request(
                "DELETE",
                "sign/schedule/lessons",
                json=[booking.id],
                before_post=before_delete,
            )
            if (
                not isinstance(result, list)
                or not all(_positive_id(item) for item in result)
                or booking.id not in result
            ):
                raise SportError("Исход отмены неизвестен. Проверьте запись в My ITMO.")
        else:
            await self._request(
                "DELETE",
                f"sign/schedule/lesson_groups/{booking.lesson_group_id}",
                before_post=before_delete,
            )

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
            result = await self._request(
                "POST",
                "sign/schedule/lessons",
                json=[lesson_id],
                enrollment=enrollment,
                before_post=before_post,
            )
            if isinstance(result, list) and len(result) == 1 and type(result[0]) is int and result[0] == lesson_id:
                return Enrollment("booked")
            return Enrollment("uncertain")
        except ApiRefusal as exc:
            if exc.code == 135 or exc.no_places:
                return Enrollment("waiting")
            if exc.code in (401, 403, 404):
                return Enrollment("uncertain")
            return Enrollment("rejected", exc.code)
