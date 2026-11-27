"""Локальный макет TUI для записи demo.tape; без учётной записи и сети."""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from textual.widgets import DataTable

import kronbot.tui as tui
from kronbot.agent import MSK
from kronbot.sport import Booking, Lesson, SportClient
from kronbot.state import StateStore


def main():
    now = datetime(2030, 3, 18, 12, tzinfo=MSK)
    lessons = []
    for index, (name, room, hour, places) in enumerate((
        ("Плавание", "Бассейн", 18, 3),
        ("Бадминтон", "Спортивный зал", 17, 2),
        ("Йога", "Зал 2", 19, 0),
    ), start=1):
        start = (now + timedelta(days=index)).replace(hour=hour, minute=0)
        lessons.append(Lesson(
            500000 + index, name, room, start, 1, 2, places, False, False, True,
            section_id=index, lesson_level=1, building_id=1, room_id=index,
            time_slot_id=index, teacher_isu=1000 + index,
            ends_at=start + timedelta(hours=1),
        ))

    with tempfile.TemporaryDirectory(prefix="kronbot-demo-") as directory:
        with StateStore(Path(directory) / "state") as store:
            store.data["session"] = {
                "sub": "demo", "isu": "123456", "access_token": "demo",
                "access_expires_at": int((now + timedelta(days=1)).timestamp()),
                "refresh_token": None, "refresh_expires_at": None,
            }
            transport = httpx.MockTransport(lambda request: httpx.Response(503))
            sport = SportClient(store, now=now.timestamp, transport=transport)
            sport.sport_types = AsyncMock(return_value=[])
            sport.account_isu = AsyncMock(return_value=None)
            app = tui.KronbotApp(store, sport=sport, now=now.timestamp)

            async def refresh(*, allow_booking, only_lesson_id=None):
                app.agent.snapshot = lessons
                app.agent.last_check = now.timestamp()
                app.call_after_refresh(app.query_one("#schedule", DataTable).focus)

            async def no_update():
                return None

            app.agent.check = refresh
            app.agent.bookings = AsyncMock(return_value=[
                Booking(500004, (now + timedelta(days=5)).replace(hour=18),
                        "Волейбол", "Зал 1", 1, None),
            ])
            app._check_update = no_update
            app.run()


if __name__ == "__main__":
    main()
