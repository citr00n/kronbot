"""Очередь и один последовательный цикл сверки и записи."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

from .auth import AuthError, LoginRequired
from .sport import Booking, HttpUnauthorized, SportError, forecast_identity
from .state import StateError
from . import notifications

MSK = timezone(timedelta(hours=3))

def opening_window(store, now) -> bool:
    """Начать единственное за день окно быстрого опроса, сохранив его до сети."""
    session = store.data["session"]
    if (session is None or store.data["background_paused"]
            or not any(item["status"] == "waiting"
                       for item in store.data["queues"].get(session["sub"], []))):
        return False
    day = datetime.fromtimestamp(now, MSK).date().isoformat()
    previous = store.data["opening_window"]
    if previous is not None and previous["day"] >= day:
        return previous["day"] == day and previous["until"] - 300 <= now < previous["until"]
    store.data["opening_window"] = {"day": day, "until": int(now) + 300}
    try:
        store.save()
    except StateError:
        store.data["opening_window"] = previous
        raise
    return True


def opening_wait(now: float) -> float:
    """Резервировать полночь перед первым автоциклом с ожидающей очередью."""
    local_now = datetime.fromtimestamp(now, MSK)
    tomorrow = local_now.date() + timedelta(days=1)
    midnight = datetime.combine(tomorrow, datetime.min.time(), MSK).timestamp()
    remaining = midnight - now
    return remaining if 0 < remaining < 120 else 0.0


def auto_delay(now: float, minimum: float = 120.0, *, forced: bool = False) -> float:
    """Задержка после автоцикла; форсированный режим не резервирует полночь."""
    if forced:
        return max(10.0, minimum)
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
        self._notification_events = []

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
            "prototype_ends_at": lesson.ends_at.isoformat() if lesson.ends_at else None,
            "predicted_ends_at": (lesson.ends_at + timedelta(days=14)).isoformat() if lesson.ends_at else None,
            "teacher_name": lesson.teacher_name,
            "room_name": lesson.room_name,
        }
        item = {
            "lesson_id": lesson.id, "section_name": lesson.section_name,
            "starts_at": target.isoformat(), "status": "waiting",
            "refusal_code": None, "forecast": forecast,
        }
        queue.append(item)
        sub = self.store.data["session"]["sub"]
        histories = self.store.data["forecast_reports"]
        old_reports = histories.get(sub)
        reports = [report for report in (old_reports or [])
                   if (report["prototype_id"], report["starts_at"]) != (lesson.id, item["starts_at"])]
        reports.append({"prototype_id": lesson.id, "starts_at": item["starts_at"],
                        "status": "not_checked", "checked_at": None,
                        "real_id": None, "differences": []})
        histories[sub] = reports
        self._trim_reports(queue)
        try:
            self.store.save()
        except StateError:
            queue.remove(item)
            if old_reports is None:
                histories.pop(sub, None)
            else:
                histories[sub] = old_reports
            raise

    def forecast_diagnostics(self):
        session = self.store.data["session"]
        if session is None:
            raise LoginRequired("Нужен новый вход.")
        reports = [dict(report, differences=list(report["differences"]))
                   for report in self.store.data["forecast_reports"].get(session["sub"], [])]
        keys = {(report["prototype_id"], report["starts_at"]) for report in reports}
        for item in self.queue():
            forecast = item.get("forecast")
            if forecast and (forecast["prototype_lesson_id"], item["starts_at"]) not in keys:
                reports.append({"prototype_id": forecast["prototype_lesson_id"],
                                "starts_at": item["starts_at"], "status": "not_checked",
                                "checked_at": None, "real_id": None, "differences": []})
        return reports

    @staticmethod
    def _matches_forecast(item, lesson):
        return forecast_identity(lesson) == (
            item["forecast"]["section_id"], 1, 2, datetime.fromisoformat(item["starts_at"])
        )

    @staticmethod
    def _possible_competitor(raw, item):
        if not isinstance(raw, dict):
            return True
        forecast = item["forecast"]
        for key, expected in (("section_id", forecast["section_id"]), ("section_level", 1)):
            value = raw.get(key)
            if type(value) is int and value > 0 and value != expected:
                return False
        typ = raw.get("type_id")
        if type(typ) is int and typ > 0 and typ not in (2, 6):
            return False
        try:
            starts = datetime.fromisoformat(raw["date"])
            if starts.utcoffset() is not None and starts != datetime.fromisoformat(item["starts_at"]):
                return False
        except (KeyError, TypeError, ValueError):
            pass
        return True

    @staticmethod
    def _forecast_differences(item, lesson):
        forecast = item["forecast"]
        fields = (
            ("teacher_isu", "teacher_isu", "преподаватель (ИСУ)"),
            ("teacher_name", "teacher_name", "имя преподавателя"),
            ("building_id", "building_id", "здание"),
            ("room_id", "room_id", "зал (ID)"),
            ("room_name", "room_name", "название зала"),
            ("lesson_level", "lesson_level", "уровень занятия"),
            ("time_slot_id", "time_slot_id", "временной слот"),
            ("predicted_ends_at", "ends_at", "время окончания"),
        )
        differences = []
        if forecast["type_id"] != lesson.type_id:
            differences.append(f"тип занятия ({forecast['type_id']} → {lesson.type_id})")
        for key, attr, label in fields:
            old, new = forecast[key], getattr(lesson, attr)
            if key == "predicted_ends_at":
                old = datetime.fromisoformat(old) if old else None
            if old != new:
                differences.append(label)
        if item["section_name"] != lesson.section_name:
            differences.append("название секции")
        return differences

    def _bind_forecasts(self, queue, snapshot, end=None):
        sub = self.store.data["session"]["sub"]
        if end is None:
            end = datetime.fromtimestamp(self.now(), MSK).date() + timedelta(days=14)
        histories = self.store.data["forecast_reports"]
        old_reports = histories.get(sub)
        reports = [dict(report, differences=list(report["differences"]))
                   for report in (old_reports or [])]
        original = queue[:]
        changes = []
        blocked = set()
        events = []
        for item in original:
            forecast = item.get("forecast")
            if not forecast or item["status"] != "waiting":
                continue
            target = datetime.fromisoformat(item["starts_at"])
            matches = [lesson for lesson in snapshot if self._matches_forecast(item, lesson)]
            broken = any(self._possible_competitor(raw, item)
                         for raw in getattr(self.sport, "forecast_invalid_rows", []))
            if target.astimezone(MSK).date() > end:
                status = "not_open"
            elif len(matches) > 1:
                status = "ambiguous"
            elif broken:
                status = "incomplete"
            elif not matches:
                status = "no_match"
            elif forecast["bound"] and item["lesson_id"] != matches[0].id:
                status = "ambiguous"
            else:
                status = "matched"
            real_id, differences = None, []
            if status == "matched":
                lesson = matches[0]
                existing = next((other for other in queue if other is not item
                                 and not _unbound_forecast(other)
                                 and other["lesson_id"] == lesson.id), None)
                if existing is not None:
                    status = "ambiguous"
                else:
                    real_id = lesson.id
                    differences = self._forecast_differences(item, lesson)
                    if not forecast["bound"]:
                        changes.append((item, item["lesson_id"]))
                        item["lesson_id"] = lesson.id
                        forecast["bound"] = True
                        if differences:
                            events.append("kronbot: прогноз связан с занятием; изменились: "
                                          + ", ".join(differences) + ".")
            if status != "matched":
                blocked.add(id(item))
            report = {"prototype_id": forecast["prototype_lesson_id"], "starts_at": item["starts_at"],
                      "status": status, "checked_at": int(self.now()), "real_id": real_id,
                      "differences": differences}
            reports = [old for old in reports if (old["prototype_id"], old["starts_at"])
                       != (report["prototype_id"], report["starts_at"])]
            reports.append(report)
        active = {(item["forecast"]["prototype_lesson_id"], item["starts_at"])
                  for item in queue if item.get("forecast")}
        archived = [report for report in reports
                    if (report["prototype_id"], report["starts_at"]) not in active]
        kept = {id(report) for report in archived[-200:]}
        histories[sub] = [report for report in reports
                          if (report["prototype_id"], report["starts_at"]) in active
                          or id(report) in kept]
        try:
            self.store.save()
        except StateError:
            if old_reports is None:
                histories.pop(sub, None)
            else:
                histories[sub] = old_reports
            for item, ident in changes:
                item["lesson_id"] = ident
                item["forecast"]["bound"] = False
            raise
        self._notification_events.extend(events)
        return blocked

    def _trim_reports(self, queue):
        histories = self.store.data["forecast_reports"]
        sub = self.store.data["session"]["sub"]
        old = histories.get(sub)
        if old is None:
            return None
        active = {(item["forecast"]["prototype_lesson_id"], item["starts_at"])
                  for item in queue if item.get("forecast")}
        archived = [report for report in old
                    if (report["prototype_id"], report["starts_at"]) not in active]
        keep = {id(report) for report in archived[-200:]}
        histories[sub] = [report for report in old
                          if (report["prototype_id"], report["starts_at"]) in active
                          or id(report) in keep]
        return old

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
        old_reports = self._trim_reports(queue)
        try:
            self.store.save()
        except StateError:
            queue.insert(index, item)
            if old_reports is not None:
                self.store.data["forecast_reports"][self.store.data["session"]["sub"]] = old_reports
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
        old_reports = self._trim_reports(queue)
        try:
            self.store.save()
        except StateError:
            queue[:] = original
            if old_reports is not None:
                self.store.data["forecast_reports"][self.store.data["session"]["sub"]] = old_reports
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
                old_reports = self._trim_reports(queue)
                try:
                    self.store.save()
                except Exception:
                    queue[:] = protected
                    if old_reports is not None:
                        self.store.data["forecast_reports"][sub] = old_reports
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
            blocked_forecasts = self._bind_forecasts(queue, snapshot, end)
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
                if id(item) not in blocked_forecasts or item["status"] == "uncertain"
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
                    id(item) in blocked_forecasts or not item["forecast"]["bound"]
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
