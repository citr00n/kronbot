"""Очередь и один последовательный цикл сверки и записи."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

from .auth import LoginRequired
from .sport import HttpUnauthorized, SportError

MSK = timezone(timedelta(hours=3))


class QueueError(Exception):
    pass


class Agent:
    def __init__(self, store, sport, *, now=time.time, monotonic=time.monotonic):
        self.store, self.sport, self.now, self.monotonic = store, sport, now, monotonic
        self.snapshot = []
        self.last_check = None
        self.last_auto_finished = None
        self.booking_enabled = True
        self.lock = asyncio.Lock()

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
        if self.sport.in_flight == lesson_id:
            raise QueueError("Запись уже отправлена; дождитесь ответа.")
        queue = self.queue()
        item = next((x for x in queue if x["lesson_id"] == lesson_id), None)
        if item is None or item["status"] == "uncertain" and not verified:
            raise QueueError("Операция неприменима. Проверьте запись в My ITMO.")
        queue.remove(item)
        self.store.save()

    def retry(self, lesson_id, *, verified=False):
        if self.sport.in_flight == lesson_id:
            raise QueueError("Запись уже отправлена; дождитесь ответа.")
        item = next((x for x in self.queue() if x["lesson_id"] == lesson_id), None)
        if not verified or item is None or item["status"] != "uncertain" or datetime.fromisoformat(item["starts_at"]).timestamp() <= self.now():
            raise QueueError("Операция неприменима. Проверьте запись в My ITMO.")
        item["status"] = "waiting"
        self.store.save()

    async def check(self, *, allow_booking: bool, only_lesson_id: int | None = None):
        async with self.lock:
            session = self.store.data["session"]
            if session is None:
                raise LoginRequired("Нужен новый вход.")
            sub = session["sub"]
            queue = self.queue()
            today = datetime.fromtimestamp(self.now(), MSK).date()
            end = today + timedelta(days=6)
            snapshot = await self.sport.schedule(today, end)
            self.snapshot = snapshot
            self.last_check = self.now()
            by_id = {lesson.id: lesson for lesson in snapshot}
            if only_lesson_id is not None and not any(x["lesson_id"] == only_lesson_id for x in queue):
                lesson = by_id.get(only_lesson_id)
                if lesson is None:
                    raise QueueError("Занятие не найдено в расписании.")
                if lesson.signed:
                    return
                self.add(lesson)
            now = self.now()
            for item in queue:
                lesson = by_id.get(item["lesson_id"])
                if item["status"] in ("waiting", "uncertain") and lesson and lesson.signed:
                    item["status"] = "booked"
                elif item["status"] == "waiting" and datetime.fromisoformat(item["starts_at"]).timestamp() <= now:
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
            for item in queue:
                if item["status"] in ("waiting", "uncertain") and item["lesson_id"] in ids:
                    item["status"] = "booked"
            self.store.save()
            if not allow_booking:
                return
            categories = {}
            for item, lesson in candidates:
                if item["status"] != "waiting":
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
                item["status"], item["refusal_code"] = result.status, result.refusal_code
                self.store.save()
                return
