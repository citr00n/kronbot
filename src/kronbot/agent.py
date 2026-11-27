"""Очередь и один последовательный цикл сверки и записи."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

from .auth import AuthError, LoginRequired
from .sport import Booking, HttpUnauthorized, SportError
from .state import StateError

MSK = timezone(timedelta(hours=3))

def opening_wait(now: float) -> float:
    """Резервировать полночь перед первым автоциклом с ожидающей очередью."""
    local_now = datetime.fromtimestamp(now, MSK)
    tomorrow = local_now.date() + timedelta(days=1)
    midnight = datetime.combine(tomorrow, datetime.min.time(), MSK).timestamp()
    remaining = midnight - now
    return remaining if 0 < remaining < 120 else 0.0


def auto_delay(now: float, minimum: float = 120.0) -> float:
    """Задержка после автоцикла с резервированием открытия в полночь МСК."""
    local_now = datetime.fromtimestamp(now, MSK)
    tomorrow = local_now.date() + timedelta(days=1)
    midnight = datetime.combine(tomorrow, datetime.min.time(), MSK).timestamp()
    remaining = midnight - now
    delay = max(120.0, minimum)
    # Когда цикл закончился менее чем за 120 секунд до полуночи, проверить
    # ровно в полночь невозможно без нарушения обязательного интервала.
    if delay <= remaining < delay + 120:
        return remaining
    return delay


class QueueError(Exception):
    pass


class Agent:
    def __init__(self, store, sport, *, now=time.time, monotonic=time.monotonic):
        self.store, self.sport, self.now, self.monotonic = store, sport, now, monotonic
        self.snapshot = []
        self.last_check = None
        self.last_auto_finished = None
        self.booking_enabled = True
        self.cancel_enabled = True
        self.lock = asyncio.Lock()
        self.cancelling_ids = set()
        self.confirmed_ids: set[int] = set()

    def queue(self):
        session = self.store.data["session"]
        if session is None:
            raise LoginRequired("Нужен новый вход.")
        return self.store.data["queues"].setdefault(session["sub"], [])

    def add(self, lesson):
        now = datetime.fromtimestamp(self.now(), timezone.utc)
        if lesson.starts_at <= now or lesson.section_level != 1 or lesson.type_id == 1 or lesson.signed:
            raise QueueError("Занятие недоступно для добавления.")
        queue = self.queue()
        if any(item["lesson_id"] == lesson.id for item in queue):
            raise QueueError("Занятие уже есть в очереди.")
        queue.append({"lesson_id": lesson.id, "section_name": lesson.section_name, "starts_at": lesson.starts_at.isoformat(), "status": "waiting", "refusal_code": None})
        self.store.save()

    def remove(self, lesson_id, *, verified=False):
        if self.sport.in_flight == lesson_id or lesson_id in self.cancelling_ids:
            raise QueueError("Запись уже отправлена; дождитесь ответа.")
        queue = self.queue()
        item = next((x for x in queue if x["lesson_id"] == lesson_id), None)
        if item is None or item["status"] == "uncertain" and not verified:
            raise QueueError("Операция неприменима. Проверьте запись в My ITMO.")
        queue.remove(item)
        self.store.save()

    def retry(self, lesson_id, *, verified=False):
        if self.sport.in_flight == lesson_id or lesson_id in self.cancelling_ids:
            raise QueueError("Запись уже отправлена; дождитесь ответа.")
        item = next((x for x in self.queue() if x["lesson_id"] == lesson_id), None)
        if not verified or item is None or item["status"] != "uncertain" or datetime.fromisoformat(item["starts_at"]).timestamp() <= self.now():
            raise QueueError("Операция неприменима. Проверьте запись в My ITMO.")
        item["status"] = "waiting"
        self.store.save()

    def _confirm(self, queue, ids):
        confirmed = [item for item in queue if item["lesson_id"] in ids]
        if not confirmed:
            return
        original = queue[:]
        queue[:] = [item for item in queue if item["lesson_id"] not in ids]
        try:
            self.store.save()
        except StateError:
            queue[:] = original
            raise
        self.confirmed_ids.update(item["lesson_id"] for item in confirmed)

    async def bookings(self) -> list[Booking]:
        async with self.lock:
            return [
                booking
                for booking in await self._bookings()
                if (booking.ends_at or booking.starts_at).timestamp() > self.now()
            ]

    async def _bookings(self) -> list[Booking]:
        self.queue()
        today = datetime.fromtimestamp(self.now(), MSK).date()
        return await self.sport.bookings(today, today + timedelta(days=364))

    async def cancel_booking(self, booking: Booking) -> None:
        if (
            not isinstance(booking, Booking)
            or type(booking.id) is not int
            or booking.id <= 0
            or type(booking.section_level) is not int
            or booking.section_level not in (1, 2, 3, 4)
            or (
                booking.lesson_group_id is not None
                and (
                    type(booking.lesson_group_id) is not int
                    or booking.lesson_group_id <= 0
                )
            )
            or (booking.section_level != 1 and booking.lesson_group_id is None)
            or not isinstance(booking.starts_at, datetime)
            or booking.starts_at.utcoffset() is None
            or booking.starts_at.timestamp() <= self.now()
        ):
            raise QueueError(
                "Отмена этой записи невозможна. Проверьте запись в My ITMO."
            )
        async with self.lock:
            session = self.store.data["session"]
            if session is None:
                raise LoginRequired("Нужен новый вход.")
            sub = session["sub"]
            current = await self._bookings()
            selected = next((item for item in current if item.id == booking.id), None)
            if (
                selected is None
                or selected.section_level != booking.section_level
                or selected.lesson_group_id != booking.lesson_group_id
                or selected.starts_at != booking.starts_at
                or self.store.data["session"] is None
                or self.store.data["session"]["sub"] != sub
                or selected.starts_at.timestamp() <= self.now()
            ):
                raise QueueError("Запись изменилась. Обновите календарь My ITMO.")
            if not self.cancel_enabled:
                raise QueueError("Отписка остановлена до отправки.")
            affected = (
                [selected]
                if selected.section_level == 1
                else [
                    item
                    for item in current
                    if item.lesson_group_id == selected.lesson_group_id
                ]
            )
            ids = {item.id for item in affected}
            queue = self.queue()
            self.cancelling_ids = ids
            try:
                existing = {item["lesson_id"]: item for item in queue}
                for item in affected:
                    if item.id not in existing:
                        entry = {
                            "lesson_id": item.id,
                            "section_name": item.section_name,
                            "starts_at": item.starts_at.isoformat(),
                            "status": "uncertain",
                            "refusal_code": None,
                        }
                        queue.append(entry)
                    else:
                        existing[item.id]["status"] = "uncertain"
                        existing[item.id]["refusal_code"] = None
                self.store.save()
                protected = list(queue)

                def permitted():
                    active = self.store.data["session"]
                    return (
                        self.cancel_enabled
                        and active is not None
                        and active["sub"] == sub
                        and selected.starts_at.timestamp() > self.now()
                        and all(
                            any(
                                entry["lesson_id"] == ident
                                and entry["status"] == "uncertain"
                                for entry in queue
                            )
                            for ident in ids
                        )
                    )

                try:
                    await self.sport.cancel_booking(selected, before_delete=permitted)
                except (AuthError, SportError, StateError) as exc:
                    exc.args = ("Исход отмены неизвестен. Проверьте запись в My ITMO.",)
                    raise
                queue[:] = [entry for entry in queue if entry["lesson_id"] not in ids]
                try:
                    self.store.save()
                except Exception:
                    queue[:] = protected
                    raise QueueError(
                        "Отмена подтверждена, но не удалось сохранить очередь. Проверьте запись в My ITMO."
                    ) from None
            finally:
                self.cancelling_ids = set()

    async def check(self, *, allow_booking: bool, only_lesson_id: int | None = None):
        async with self.lock:
            self.confirmed_ids = set()
            session = self.store.data["session"]
            if session is None:
                raise LoginRequired("Нужен новый вход.")
            sub = session["sub"]
            queue = self.queue()
            today = datetime.fromtimestamp(self.now(), MSK).date()
            # Включаем день, открывающийся ровно через 14 суток.
            end = today + timedelta(days=14)
            snapshot = await self.sport.schedule(today, end)
            self.snapshot = snapshot
            self.last_check = self.now()
            by_id = {lesson.id: lesson for lesson in snapshot}
            if only_lesson_id is not None and not any(x["lesson_id"] == only_lesson_id for x in queue):
                lesson = by_id.get(only_lesson_id)
                if lesson is None:
                    raise QueueError("Занятие не найдено в расписании.")
                if lesson.signed:
                    self.confirmed_ids.add(lesson.id)
                    return
                self.add(lesson)
            now = self.now()
            signed_ids = {
                item["lesson_id"]
                for item in queue
                if (lesson := by_id.get(item["lesson_id"])) is not None
                and lesson.signed
            }
            self._confirm(queue, signed_ids)
            for item in queue:
                if (
                    item["status"] == "waiting"
                    and datetime.fromisoformat(item["starts_at"]).timestamp() <= now
                ):
                    item["status"] = "expired"
            self.store.save()
            candidates = []
            for item in queue:
                lesson = by_id.get(item["lesson_id"])
                if (item["status"] == "waiting" and lesson is not None and lesson.starts_at.timestamp() > now
                        and lesson.section_level == 1 and lesson.type_id != 1 and lesson.available > 0
                        and not lesson.signed and lesson.can_sign_in
                        and (only_lesson_id is None or lesson.id == only_lesson_id)):
                    candidates.append((item, lesson))
            uncertain = [item for item in queue if item["status"] == "uncertain"
                         and (today - datetime.fromisoformat(item["starts_at"]).astimezone(MSK).date()).days <= 7]
            if not uncertain and (not allow_booking or not candidates):
                return
            start = min([today] + [datetime.fromisoformat(item["starts_at"]).astimezone(MSK).date() for item in uncertain])
            ids = await self.sport.calendar_ids(start, end)
            self._confirm(
                queue,
                {
                    item["lesson_id"]
                    for item in queue
                    if item["lesson_id"] in ids
                },
            )
            if not allow_booking:
                return
            categories = {}
            for item, lesson in candidates:
                if item not in queue or item["status"] != "waiting":
                    continue
                category = lesson.type_id == 5
                if category not in categories:
                    categories[category] = await self.sport.can_spend_attempt(lesson.type_id)
                if not categories[category]:
                    continue
                def permitted():
                    current = self.store.data["session"]
                    return (self.booking_enabled and current is not None and current["sub"] == sub
                            and item in queue and item["status"] == "waiting"
                            and lesson.starts_at.timestamp() > self.now())
                if not permitted():
                    return
                try:
                    result = await self.sport.enroll(lesson.id, item, before_post=permitted)
                except HttpUnauthorized:
                    item["status"] = "waiting"
                    self.store.save()
                    await self.sport.refresh()
                    return
                except SportError:
                    # A reserved request remains uncertain on any unknown outcome.
                    raise
                if result.status == "booked":
                    self._confirm(queue, {lesson.id})
                else:
                    item["status"], item["refusal_code"] = (
                        result.status,
                        result.refusal_code,
                    )
                    self.store.save()
                return
