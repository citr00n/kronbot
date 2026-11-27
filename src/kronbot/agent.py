"""Очередь и один последовательный цикл сверки и записи."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

from .auth import AuthError, LoginRequired
from .sport import Booking, HttpUnauthorized, SportError, forecast_identity
from .state import StateError
from . import notifications

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

def _unbound_forecast(item):
    forecast = item.get("forecast")
    return bool(forecast and not forecast["bound"])


class Agent:
    def __init__(self, store, sport, *, now=time.time, monotonic=time.monotonic):
        self.store, self.sport, self.now, self.monotonic = store, sport, now, monotonic
        self.snapshot = []
        self.last_check = None
        self.last_auto_finished = None
        self.booking_enabled = True
        self.cancel_enabled = True
        self.lock = asyncio.Lock()
        self._cycle_lock = asyncio.Lock()
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
        if any(item["lesson_id"] == lesson.id and not _unbound_forecast(item) for item in queue):
            raise QueueError("Занятие уже есть в очереди.")
        queue.append({"lesson_id": lesson.id, "section_name": lesson.section_name, "starts_at": lesson.starts_at.isoformat(), "status": "waiting", "refusal_code": None})
        try:
            self.store.save()
        except StateError:
            queue.pop()
            raise

    def add_forecast(self, lesson):
        identity = forecast_identity(lesson)
        target = lesson.starts_at + timedelta(days=14)
        if identity is None or lesson.section_level != 1 or target.timestamp() <= self.now():
            raise QueueError("Для этого занятия нельзя создать надёжный прогноз.")
        queue = self.queue()
        if any(item.get("forecast", {}).get("prototype_lesson_id") == lesson.id for item in queue):
            raise QueueError("Занятие уже есть в очереди.")
        forecast = {
            "bound": False,
            "prototype_lesson_id": lesson.id,
            "section_id": lesson.section_id,
            "teacher_isu": lesson.teacher_isu,
            "building_id": lesson.building_id,
            "room_id": lesson.room_id,
            "section_level": lesson.section_level,
            "lesson_level": lesson.lesson_level,
            "type_id": lesson.type_id,
            "time_slot_id": lesson.time_slot_id,
            "prototype_starts_at": lesson.starts_at.isoformat(),
            "prototype_ends_at": lesson.ends_at.isoformat(),
            "predicted_ends_at": (lesson.ends_at + timedelta(days=14)).isoformat(),
            "teacher_name": lesson.teacher_name,
            "room_name": lesson.room_name,
        }
        item = {
            "lesson_id": lesson.id, "section_name": lesson.section_name,
            "starts_at": target.isoformat(), "status": "waiting",
            "refusal_code": None, "forecast": forecast,
        }
        queue.append(item)
        try:
            self.store.save()
        except StateError:
            queue.remove(item)
            raise

    @staticmethod
    def _matches_forecast(item, lesson):
        forecast = item["forecast"]
        return forecast_identity(lesson) == (
            forecast["section_id"], forecast["teacher_isu"],
            None if forecast["room_id"] == -1 else forecast["building_id"],
            forecast["room_id"],
            forecast["section_level"], forecast["lesson_level"],
            forecast["type_id"], forecast["time_slot_id"],
            datetime.fromisoformat(item["starts_at"]),
            datetime.fromisoformat(forecast["predicted_ends_at"]),
        )

    def _bind_forecasts(self, queue, snapshot):
        # A collision keeps the existing entry, especially an uncertain one.
        original = queue[:]
        if getattr(self.sport, "forecast_incomplete", False):
            return
        ambiguous_times = getattr(self.sport, "forecast_ambiguous_times", set())
        changed = []
        for item in original:
            forecast = item.get("forecast")
            if not forecast or forecast["bound"] or item["status"] != "waiting":
                continue
            target = datetime.fromisoformat(item["starts_at"])
            if target in ambiguous_times or any(
                lesson.starts_at == target and forecast_identity(lesson) is None
                for lesson in snapshot
            ):
                continue
            matches = [lesson for lesson in snapshot if self._matches_forecast(item, lesson)]
            if len(matches) != 1:
                continue
            lesson = matches[0]
            existing = next(
                (other for other in queue if other is not item
                 and not _unbound_forecast(other) and other["lesson_id"] == lesson.id),
                None,
            )
            if existing is not None:
                if existing["status"] in {"rejected", "expired"}:
                    continue
                queue.remove(item)
            else:
                changed.append((item, item["lesson_id"]))
                item["lesson_id"] = lesson.id
                forecast["bound"] = True
        if queue == original and not changed:
            return
        try:
            self.store.save()
        except StateError:
            queue[:] = original
            for item, ident in changed:
                item["lesson_id"] = ident
                item["forecast"]["bound"] = False
            raise

    def remove(self, lesson_id, *, verified=False, forecast=None):
        queue = self.queue()
        matches = [x for x in queue if x["lesson_id"] == lesson_id
                   and (forecast is None or _unbound_forecast(x) == forecast)]
        if len(matches) != 1:
            raise QueueError("Укажите однозначную запись очереди.")
        item = matches[0]
        if not _unbound_forecast(item) and (
            self.sport.in_flight == lesson_id or lesson_id in self.cancelling_ids
        ):
            raise QueueError("Запись уже отправлена; дождитесь ответа.")
        if item["status"] == "uncertain" and not verified:
            raise QueueError("Операция неприменима. Проверьте запись в My ITMO.")
        index = queue.index(item)
        queue.remove(item)
        try:
            self.store.save()
        except StateError:
            queue.insert(index, item)
            raise

    def retry(self, lesson_id, *, verified=False, forecast=None):
        queue = self.queue()
        matches = [x for x in queue if x["lesson_id"] == lesson_id
                   and (forecast is None or _unbound_forecast(x) == forecast)]
        if len(matches) > 1:
            raise QueueError("Укажите однозначную запись очереди.")
        item = matches[0] if matches else None
        if item is not None and not _unbound_forecast(item) and (
            self.sport.in_flight == lesson_id or lesson_id in self.cancelling_ids
        ):
            raise QueueError("Запись уже отправлена; дождитесь ответа.")
        if not verified or item is None or item["status"] != "uncertain" or datetime.fromisoformat(item["starts_at"]).timestamp() <= self.now():
            raise QueueError("Операция неприменима. Проверьте запись в My ITMO.")
        item["status"] = "waiting"
        try:
            self.store.save()
        except StateError:
            item["status"] = "uncertain"
            raise

    def _confirm(self, queue, ids):
        confirmed = [item for item in queue if item["lesson_id"] in ids
                     and not _unbound_forecast(item)]
        if not confirmed:
            return
        original = queue[:]
        queue[:] = [item for item in queue
                    if item["lesson_id"] not in ids or _unbound_forecast(item)]
        try:
            self.store.save()
        except StateError:
            queue[:] = original
            raise
        self.confirmed_ids.update(item["lesson_id"] for item in confirmed)
        self._notification_events.extend(
            "kronbot: запись на занятие подтверждена." for _ in confirmed
        )

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
                existing = {item["lesson_id"]: item for item in queue
                            if not _unbound_forecast(item)}
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
                queue[:] = [entry for entry in queue
                            if entry["lesson_id"] not in ids or _unbound_forecast(entry)]
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
        async with self._cycle_lock:
            await self._check_with_notifications(
                allow_booking=allow_booking, only_lesson_id=only_lesson_id
            )

    async def _check_with_notifications(self, *, allow_booking, only_lesson_id):
        # Доставка только после завершения всех решений и POST текущего цикла.
        self._notification_events = []
        self._cycle_error = None
        state_failed = False
        try:
            await self._check(allow_booking=allow_booking, only_lesson_id=only_lesson_id)
        except (SportError, AuthError) as exc:
            self._cycle_error = type(exc).__name__
            raise
        except StateError:
            state_failed = True
            raise
        finally:
            try:
                for message in self._notification_events:
                    await notifications.send(self.store, message)
                # При сбое сохранения нельзя случайно сохранить незавершённое
                # состояние очереди вместе с настройкой дедупликации.
                if not state_failed:
                    await notifications.error(self.store, self._cycle_error)
            except Exception:
                pass

    async def _check(self, *, allow_booking: bool, only_lesson_id: int | None = None):
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
            originally_targeted = only_lesson_id is not None and any(
                item["lesson_id"] == only_lesson_id and not _unbound_forecast(item)
                for item in queue
            )
            self._bind_forecasts(queue, snapshot)
            if originally_targeted and not any(
                item["lesson_id"] == only_lesson_id and not _unbound_forecast(item)
                for item in queue
            ):
                return
            if only_lesson_id is not None and not any(
                x["lesson_id"] == only_lesson_id and not _unbound_forecast(x) for x in queue
            ):
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
                if not (item.get("forecast") and not item["forecast"]["bound"])
                if (lesson := by_id.get(item["lesson_id"])) is not None
                and (not item.get("forecast") or self._matches_forecast(item, lesson))
                and lesson.signed
            }
            self._confirm(queue, signed_ids)
            missed_forecasts = []
            for item in queue:
                if (
                    item["status"] == "waiting"
                    and datetime.fromisoformat(item["starts_at"]).timestamp() <= now
                ):
                    item["status"] = "expired"
                    if _unbound_forecast(item):
                        missed_forecasts.append(item)
            self.store.save()
            self._notification_events.extend(
                "kronbot: прогноз занятия не подтвердился к началу занятия."
                for _ in missed_forecasts
            )
            candidates = []
            for item in queue:
                lesson = by_id.get(item["lesson_id"])
                if item.get("forecast") and (
                    not item["forecast"]["bound"]
                    or lesson is None
                    or not self._matches_forecast(item, lesson)
                ):
                    continue
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
                    if not (item.get("forecast") and not item["forecast"]["bound"])
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
                if categories.get(category) is False:
                    continue
                if not await self.sport.can_spend_attempt(lesson.type_id):
                    categories[category] = False
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
                    self._cycle_error = "unauthorized"
                    item["status"] = "waiting"
                    try:
                        self.store.save()
                    except StateError:
                        item["status"] = "uncertain"
                        raise
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
                    try:
                        self.store.save()
                    except StateError:
                        item["status"], item["refusal_code"] = "uncertain", None
                        raise
                    if result.status in {"uncertain", "rejected"}:
                        self._cycle_error = result.status
                if only_lesson_id is not None or result.status == "uncertain":
                    return
