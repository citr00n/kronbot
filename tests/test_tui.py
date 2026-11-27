from datetime import datetime, timezone
import asyncio

import httpx
import pytest
from textual.widgets import DataTable

from kronbot.state import StateStore
from kronbot.sport import SportClient
from kronbot.tui import KronbotApp


@pytest.fixture(autouse=True)
def no_account_network(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(SportClient, "account_isu", AsyncMock(return_value=None), raising=False)
    monkeypatch.setattr(SportClient, "sport_types", AsyncMock(return_value=[]), raising=False)


async def test_startup_read_only_refresh(tmp_path):
    now = 1900000000
    calls = []
    start = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
    def handler(request):
        if request.url.path.endswith("/personal/calendar"):
            calls.append(request)
            return httpx.Response(200, json={"error_code": 0, "result": []})
        calls.append(request)
        return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [{"id": 500001, "date": start, "section_name": "Секция", "room_name": "Зал", "section_level": 1, "type_id": 2, "available": 0, "signed": False, "can_sign_in": {"can_sign_in": True}}]}]})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        app = KronbotApp(store, sport=client, now=lambda: now)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert len(calls) == 2 and all(call.method == "GET" for call in calls)
            assert app.query_one("#schedule", DataTable).row_count == 1
            assert not app.auto
        await client.close()


async def test_queue_and_auto_booking(tmp_path):
    clock = {"wall": 1900000000, "mono": 0, "available": 0}
    start = datetime.fromtimestamp(clock["wall"] + 86400, timezone.utc).isoformat()
    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append(request.content)
            return httpx.Response(200, json={"error_code": 0, "result": [500001]})
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [{"id": 500001, "date": start, "section_name": "Секция", "room_name": "Зал", "section_level": 1, "type_id": 2, "available": clock["available"], "signed": False, "can_sign_in": {"can_sign_in": True}}]}]})
        if request.url.path.endswith("/personal/calendar"):
            lessons = [{"id": 500001, "date": start, "section_name": "Секция", "room_name": "Зал", "section_level": 1}] if posts else []
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": lessons}]})
        return httpx.Response(200, json={"error_code": 0, "result": {"free_attempts": 1, "can_sign_in": True}})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": clock["wall"] + 10000, "refresh_token": None, "refresh_expires_at": None}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: clock["wall"])
        app = KronbotApp(store, sport=client, now=lambda: clock["wall"], monotonic=lambda: clock["mono"])
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one("#schedule", DataTable).focus()
            await pilot.press("enter")
            assert app.agent.queue()[0]["status"] == "waiting"
            await pilot.press("r", "p")
            await pilot.pause()
            assert not posts and app.agent.queue()[0]["status"] == "waiting"
            clock["available"] = 1
            clock["wall"] += 121
            clock["mono"] += 121
            app._tick()
            await pilot.pause()
            assert posts == [b"[500001]"]
            assert app.agent.queue() == []
            assert [booking.id for booking in app.bookings_snapshot] == [500001]
            assert app.query_one("#bookings", DataTable).row_count == 1
        await client.close()


async def test_login_password_is_masked_and_escape_returns(tmp_path):
    from textual.widgets import Input, Select
    from kronbot.tui import LoginScreen
    with StateStore(tmp_path / "state") as store:
        app = KronbotApp(store)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, LoginScreen)
            app.screen.query_one("#method", Select).value = "password"
            await pilot.pause()
            secret = app.screen.query_one("#secret", Input)
            assert secret.password
            secret.value = "synthetic-password"
            await pilot.press("escape")
            await pilot.pause()
            assert secret.value == ""


@pytest.mark.parametrize(
    ("method", "phrases", "username_visible", "secret_visible"),
    [
        ("browser", ("kronbot browser install", "графическая сессия", "sandbox", "отдельном"), False, False),
        ("password", ("логин", "Пароль скрыт", "веб-вход"), True, True),
        ("refresh", ("только свой", "скрытое", "ротирован", "сессия"), False, True),
        ("access", ("только свой", "скрытое", "не продлевается"), False, True),
    ],
)
async def test_login_guide_methods_fit_terminal(method, phrases, username_visible, secret_visible):
    from textual.app import App
    from textual.widgets import Button, Input, Select, Static
    from kronbot.tui import LoginScreen

    class GuideApp(App):
        CSS = KronbotApp.CSS

        def on_mount(self):
            self.push_screen(LoginScreen())

    app = GuideApp()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = app.screen
        screen.query_one("#method", Select).value = method
        await pilot.pause()
        guide = screen.query_one("#login-guide", Static)
        for phrase in phrases:
            assert phrase in guide.content
        assert screen.query_one("#username", Input).display == username_visible
        secret = screen.query_one("#secret", Input)
        assert secret.display == secret_visible
        assert secret.password
        for widget in (guide, screen.query_one("#submit", Button), screen.query_one("#back", Button)):
            assert widget.region.bottom <= 24
            assert widget.region.height > 0
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, LoginScreen)


async def test_login_guide_secret_input_and_single_attempt(monkeypatch):
    from textual.app import App
    from textual.widgets import Input, Select
    from kronbot.tui import LoginScreen

    started = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def login(self, method, username, secret):
        calls.append((method, username, secret))
        started.set()
        await finish.wait()

    monkeypatch.setattr(LoginScreen, "_login", login)

    class GuideApp(App):
        CSS = KronbotApp.CSS

        def on_mount(self):
            self.push_screen(LoginScreen())

    app = GuideApp()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = app.screen
        screen.query_one("#method", Select).value = "access"
        await pilot.pause()
        secret = screen.query_one("#secret", Input)
        secret.focus()
        await pilot.press("q", "r", "p", "l")
        assert secret.value == "qrpl"
        await pilot.press("enter")
        await asyncio.wait_for(started.wait(), timeout=1)
        assert calls == [("access", "", "qrpl")]
        assert secret.value == ""
        await pilot.press("enter")
        assert len(calls) == 1
        await pilot.press("escape")
        assert screen.cancel.is_set()
        finish.set()
        await pilot.pause()
        assert not isinstance(app.screen, LoginScreen)


async def test_bookings_confirmation_and_account_isolation(tmp_path):
    from unittest.mock import AsyncMock
    from textual.widgets import Static, TabbedContent
    from kronbot.sport import Booking
    from kronbot.tui import ConfirmScreen

    now = 1900000000
    booking = Booking(42, datetime.fromtimestamp(now + 86400, timezone.utc), "Секция", "Зал", 1, None)
    group = Booking(43, datetime.fromtimestamp(now + 172800, timezone.utc), "Группа", "Зал", 2, 77)
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner\n[red]", "isu": "123456", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        app = KronbotApp(store, now=lambda: now)
        app.agent.check = AsyncMock()
        app.agent.bookings = AsyncMock(return_value=[booking, group])
        app.agent.cancel_booking = AsyncMock()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            status = str(app.query_one("#status", Static).render())
            assert "owner" not in status
            account = str(app.query_one("#account", Static).render())
            assert "123456" in account and "owner" not in account
            app.query_one(TabbedContent).active = "bookings-tab"
            await pilot.pause()
            table = app.query_one("#bookings", DataTable)
            assert table.row_count == 2
            table.focus()
            await pilot.press("enter")
            assert isinstance(app.screen, ConfirmScreen)
            app.agent.cancel_booking.assert_not_awaited()
            await pilot.press("escape")
            await pilot.pause()
            app.agent.cancel_booking.assert_not_awaited()
            table.move_cursor(row=1)
            await pilot.press("x")
            assert isinstance(app.screen, ConfirmScreen)
            assert "всю группу" in app.screen.message
            await pilot.click("#yes")
            await pilot.pause()
            app.agent.cancel_booking.assert_awaited_once_with(group)
            assert app._selected(table) == "43"
            store.data["session"]["sub"] = "another"
            app._render()
            assert app.bookings_snapshot == []
            assert table.row_count == 1
            assert "another" not in str(app.query_one("#status", Static).render())
            assert "another" not in str(app.query_one("#account", Static).render())
        await app.sport.close()


async def test_bookings_stale_and_missing_group_block_cancellation(tmp_path):
    from unittest.mock import AsyncMock
    from textual.widgets import TabbedContent
    from kronbot.sport import Booking

    now = 1900000000
    booking = Booking(42, datetime.fromtimestamp(now + 86400, timezone.utc), "Группа", "Зал", 2, None)
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        app = KronbotApp(store, now=lambda: now)
        app.agent.check = AsyncMock()
        app.agent.bookings = AsyncMock(return_value=[booking])
        app.agent.cancel_booking = AsyncMock()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = "bookings-tab"
            app.query_one("#bookings", DataTable).focus()
            await pilot.press("enter")
            assert "идентификатор группы" in app.error
            app.bookings_stale = True
            await pilot.press("x")
            assert "устарели" in app.error
            app.agent.cancel_booking.assert_not_awaited()
        await app.sport.close()


async def test_bookings_refresh_when_schedule_fails(tmp_path):
    from unittest.mock import AsyncMock
    from textual.widgets import TabbedContent
    from kronbot.sport import Booking, SportError

    now = 1900000000
    booking = Booking(42, datetime.fromtimestamp(now + 86400, timezone.utc), "Секция", "Зал", 1, None)
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        app = KronbotApp(store, now=lambda: now)
        app.agent.check = AsyncMock(side_effect=SportError("Расписание недоступно"))
        app.agent.bookings = AsyncMock(return_value=[booking])
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.bookings_stale
            app.query_one(TabbedContent).active = "bookings-tab"
            await pilot.press("r")
            await pilot.pause()
            app.agent.check.assert_awaited_once()
            app.agent.bookings.assert_awaited_once()
            assert app.bookings_snapshot == [booking]
            assert not app.bookings_stale
        await app.sport.close()


async def test_service_warning_visible_on_login_screen(tmp_path):
    from textual.widgets import Button, Static
    from kronbot.tui import LoginScreen
    with StateStore(tmp_path / "state") as store:
        app = KronbotApp(store, service_notice="Фоновая служба не установлена.")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, LoginScreen)
            notice = app.screen.query_one("#service-notice", Static)
            assert "не установлена" in str(notice.render())
            assert app.screen.query_one("#submit", Button).region.bottom <= 24
            await pilot.press("escape")
        await app.sport.close()


def test_tui_run_installs_service_before_opening_app(monkeypatch, tmp_path):
    from kronbot import cli, tui
    calls = []
    monkeypatch.setenv("KRONBOT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_service_notice", lambda path, *, install: calls.append((path, install)) or "Служба недоступна")
    monkeypatch.setattr(cli, "_service_running", lambda path: False)
    class FakeApp:
        def __init__(self, store, *, service_notice, service_active, service_probe):
            assert service_notice == "Служба недоступна"
            assert not service_active and not service_probe()
        def run(self):
            calls.append("ui")
    monkeypatch.setattr(tui, "KronbotApp", FakeApp)
    assert tui.run() == 0
    assert calls == [((tmp_path / "state").absolute(), True), "ui"]


async def test_schedule_filters_selection_and_account(tmp_path):
    from unittest.mock import AsyncMock
    from textual.widgets import Input, Select, Static, TabbedContent
    from kronbot.sport import Lesson

    now = 1900000000
    morning = datetime(2030, 3, 19, 7, tzinfo=timezone.utc)
    evening = datetime(2030, 3, 19, 16, tzinfo=timezone.utc)
    lessons = [
        Lesson(1, "Бассейн", "Зал Север", morning, 1, 2, 0, False, False, False),
        Lesson(2, "Бег", "Арена Юг", evening, 1, 5, 3, False, False, True),
    ]
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "secret-owner", "isu": None, "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        app = KronbotApp(store, now=lambda: now)
        app.agent.check = AsyncMock()
        app.agent.bookings = AsyncMock(return_value=[])
        app.sport.sport_types = AsyncMock(return_value=[(77, "Лёгкая атлетика")])
        app.sport.schedule = AsyncMock(return_value=[lessons[1]])
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.agent.snapshot = lessons
            app._render()
            table = app.query_one("#schedule", DataTable)
            table.move_cursor(row=1)
            search = app.query_one("#filter-search", Input)
            search.focus()
            await pilot.press("q", "r", "p", "l")
            assert search.value == "qrpl" and not app.auto
            search.value = "север"
            await pilot.pause()
            assert table.row_count == 1 and app._selected(table) == "1"
            search.value = ""
            await pilot.pause()
            assert app._selected(table) == "2"
            app.query_one("#filter-type", Select).value = 77
            await pilot.pause()
            assert app.sport.schedule.await_count == 1
            assert app.sport.schedule.await_args.kwargs == {"sport_type_id": 77}
            start, end = app.sport.schedule.await_args.args
            from kronbot.agent import MSK
            assert start == datetime.fromtimestamp(now, MSK).date()
            assert (end - start).days == 14
            app.query_one("#filter-sign", Select).value = "yes"
            app.query_one("#filter-places", Select).value = "yes"
            app.query_one("#filter-time", Select).value = "evening"
            await pilot.pause()
            assert table.row_count == 1 and app._selected(table) == "2"
            app.query_one("#filter-time", Select).value = "morning"
            await pilot.pause()
            assert app._selected(table) != "2"
            await pilot.click("#filter-clear")
            await pilot.pause()
            assert table.row_count == 2 and app._selected(table) == "2"
            assert table.region.height > 0 and table.region.bottom <= 24
            app.query_one(TabbedContent).active = "account-tab"
            await pilot.pause()
            account = str(app.query_one("#account", Static).render())
            assert "не указан" in account and "secret-owner" not in account
            app.query_one(TabbedContent).active = "schedule-tab"
            search.focus()
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.stopping
        await app.sport.close()


async def test_auto_cycle_reserves_midnight(tmp_path):
    from unittest.mock import AsyncMock
    from kronbot.agent import MSK

    midnight = datetime(2030, 3, 20, 0, 0, tzinfo=MSK).timestamp()
    clock = {"wall": midnight - 180, "mono": 0}
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": midnight + 10000, "refresh_token": None, "refresh_expires_at": None}
        store.data["queues"]["owner"] = [{"lesson_id": 1, "section_name": "Бег", "starts_at": datetime.fromtimestamp(midnight + 86400, timezone.utc).isoformat(), "status": "waiting", "refusal_code": None}]
        app = KronbotApp(store, now=lambda: clock["wall"], monotonic=lambda: clock["mono"])
        app.agent.check = AsyncMock()
        app.agent.bookings = AsyncMock(return_value=[])
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert app.auto_interval == 180
            assert app.agent.check.await_count == 2
            clock["mono"] = 120
            clock["wall"] = midnight - 60
            app._tick()
            await pilot.pause()
            assert app.agent.check.await_count == 2
            clock["mono"] = 180
            clock["wall"] = midnight
            app._tick()
            await pilot.pause()
            assert app.agent.check.await_count == 3
        await app.sport.close()


async def test_first_auto_cycle_waits_for_midnight_but_manual_refresh_does_not(tmp_path):
    from unittest.mock import AsyncMock
    from kronbot.agent import MSK

    midnight = datetime(2030, 3, 20, 0, 0, tzinfo=MSK).timestamp()
    clock = {"wall": midnight - 5, "mono": 0}
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": midnight + 10000, "refresh_token": None, "refresh_expires_at": None}
        store.data["queues"]["owner"] = [{"lesson_id": 1, "section_name": "Бег", "starts_at": datetime.fromtimestamp(midnight + 86400, timezone.utc).isoformat(), "status": "waiting", "refusal_code": None}]
        app = KronbotApp(store, now=lambda: clock["wall"], monotonic=lambda: clock["mono"])
        app.agent.check = AsyncMock()
        app.agent.bookings = AsyncMock(return_value=[])
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert app.agent.check.await_count == 1
            app.action_refresh()
            await pilot.pause()
            assert app.agent.check.await_count == 2
            clock["wall"] = midnight
            clock["mono"] = 5
            app._tick()
            await pilot.pause()
            assert app.agent.check.await_count == 3
            assert app.agent.check.await_args.kwargs == {"allow_booking": True}
        await app.sport.close()


async def test_isu_lookup_once_and_after_login(tmp_path):
    from unittest.mock import AsyncMock
    from textual.widgets import Static

    now = 1900000000
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "not-isu", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        app = KronbotApp(store, now=lambda: now)
        app.agent.check = AsyncMock()
        app.agent.bookings = AsyncMock(return_value=[])

        async def lookup():
            store.data["session"]["isu"] = "123456"
            return "123456"

        app.sport.account_isu = AsyncMock(side_effect=lookup)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.sport.account_isu.assert_awaited_once()
            assert "123456" in str(app.query_one("#account", Static).render())
            app.action_refresh()
            await pilot.pause()
            assert app.sport.account_isu.await_count == 1
            store.data["session"].pop("isu")
            app._login_result(True)
            await pilot.pause()
            assert app.sport.account_isu.await_count == 2
        await app.sport.close()


async def test_sport_filter_ignores_outdated_response(tmp_path):
    from unittest.mock import AsyncMock
    from textual.widgets import Select
    from kronbot.sport import Lesson

    now = 1900000000
    starts = datetime.fromtimestamp(now + 86400, timezone.utc)
    first = Lesson(1, "Секция А", "Зал", starts, 1, 2, 1, False, False, True)
    second = Lesson(2, "Секция Б", "Зал", starts, 1, 2, 1, False, False, True)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def filtered(start, end, *, sport_type_id):
        calls.append(sport_type_id)
        if sport_type_id == 10:
            started.set()
            await release.wait()
            return [first]
        return [second]

    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        app = KronbotApp(store, now=lambda: now)
        app.agent.check = AsyncMock()
        app.agent.bookings = AsyncMock(return_value=[])
        app.sport.sport_types = AsyncMock(return_value=[(10, "Плавание"), (20, "Бег")])
        app.sport.schedule = AsyncMock(side_effect=filtered)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.agent.snapshot = [first, second]
            select = app.query_one("#filter-type", Select)
            select.value = 10
            await asyncio.wait_for(started.wait(), timeout=1)
            select.value = 20
            await pilot.pause()
            assert app.filter_stale
            release.set()
            await pilot.pause()
            assert calls == [10, 20]
            assert app.agent.snapshot == [first, second]
            assert app.filter_ids == {2}
            assert app._selected(app.query_one("#schedule", DataTable)) == "2"
        await app.sport.close()


async def test_forecast_key_adds_future_lesson_and_reports_invalid(tmp_path):
    from datetime import timedelta
    from unittest.mock import AsyncMock
    from kronbot.sport import Lesson

    now = 1900000000
    starts = datetime.fromtimestamp(now + 86400, timezone.utc)
    lesson = Lesson(10, "Бег", "Зал", starts, 1, 2, 1, False, False, True,
                    section_id=1, lesson_level=1, building_id=1, room_id=2,
                    time_slot_id=3, teacher_isu=4, ends_at=starts + timedelta(hours=1))
    invalid = Lesson(11, "Плавание", "Зал", starts, 1, 2, 1, False, False, True)
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        app = KronbotApp(store, now=lambda: now)
        app.sport.sport_types = AsyncMock(return_value=[])
        app.sport.account_isu = AsyncMock(return_value=None)
        async def refresh(*, allow_booking):
            app.agent.snapshot = [lesson, invalid]
        app.agent.check = AsyncMock(side_effect=refresh)
        app.agent.bookings = AsyncMock(return_value=[])
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            table = app.query_one("#schedule", DataTable)
            table.focus()
            await pilot.press("f")
            item = app.agent.queue()[0]
            assert item["forecast"]["bound"] is False
            assert datetime.fromisoformat(item["starts_at"]) == starts + timedelta(days=14)
            assert "прогноз" in str(app.query_one("#queue", DataTable).get_row("10:forecast")[-1]).lower()
            await pilot.press("enter")
            queue = app.query_one("#queue", DataTable)
            assert queue.row_count == 2
            assert queue.get_row("10:lesson") and queue.get_row("10:forecast")
            from textual.widgets import TabbedContent
            tabs = app.query_one(TabbedContent)
            tabs.active = "queue-tab"
            await pilot.pause()
            queue.move_cursor(row=queue.get_row_index("10:lesson"))
            app.action_remove()
            assert len(app.agent.queue()) == 1 and app.agent.queue()[0].get("forecast")
            tabs.active = "schedule-tab"
            await pilot.pause()
            table.focus()
            table.move_cursor(row=1)
            await pilot.press("f")
            assert "нельзя создать" in app.error
            assert len(app.agent.queue()) == 1
            app._remove("10:forecast", True)
            assert not app.agent.queue()
        await app.sport.close()


async def test_running_service_handoff_pause_and_stopping(tmp_path):
    from unittest.mock import AsyncMock
    from kronbot.agent import MSK

    now = int(datetime(2030, 3, 20, 12, tzinfo=MSK).timestamp())
    clock = {"wall": now, "mono": 0}
    running = {"value": True}
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        store.data["queues"]["owner"] = [{"lesson_id": 1, "section_name": "Бег", "starts_at": datetime.fromtimestamp(now + 86400, timezone.utc).isoformat(), "status": "waiting", "refusal_code": None}]
        app = KronbotApp(store, now=lambda: clock["wall"], monotonic=lambda: clock["mono"],
                         service_active=True, service_probe=lambda: running["value"])
        app.sport.sport_types = AsyncMock(return_value=[])
        app.sport.account_isu = AsyncMock(return_value=None)
        app.agent.check = AsyncMock()
        app.agent.bookings = AsyncMock(return_value=[])
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.7)
            assert app.auto
            assert any(call.kwargs["allow_booking"] for call in app.agent.check.await_args_list)
            app.action_pause()
            assert not app.auto and store.data["background_paused"]
            app.action_pause()
            assert app.auto and not store.data["background_paused"]
            running["value"] = False
            clock["mono"] += 1
            app._tick()
            await app.service_probe_task
            assert not app.auto and not app.agent.booking_enabled
            assert "остановлена" in app.service_notice
        await app.sport.close()


async def test_service_pause_survives_login_and_requires_explicit_resume(tmp_path):
    from unittest.mock import AsyncMock
    from kronbot.sport import NetworkBlocked

    now = 1900000000
    clock = {"wall": now}
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        app = KronbotApp(store, now=lambda: clock["wall"], service_active=True)
        app.sport.sport_types = AsyncMock(return_value=[])
        app.sport.account_isu = AsyncMock(return_value=None)
        app.agent.check = AsyncMock()
        app.agent.bookings = AsyncMock(return_value=[])
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            store.data["blocked_until"]["my.itmo.ru"] = now + 120
            app._stop_for_error(NetworkBlocked(now + 120))
            assert store.data["background_paused"] and not app.auto
            app.action_login()
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert not app.auto and store.data["background_paused"]
            app.action_pause()
            assert not app.auto and store.data["background_paused"]
            clock["wall"] += 121
            await pilot.pause()
            assert not app.auto
            app.action_pause()
            assert app.auto and not store.data["background_paused"]
        await app.sport.close()


async def test_service_pause_during_refresh_prevents_post(tmp_path):
    from unittest.mock import AsyncMock
    from kronbot.agent import MSK

    now = int(datetime(2030, 3, 20, 12, tzinfo=MSK).timestamp())
    starts = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
    reached, release = asyncio.Event(), asyncio.Event()
    calls = {"schedule": 0, "posts": 0}
    async def handler(request):
        if request.method == "POST":
            calls["posts"] += 1
            return httpx.Response(200, json={"error_code": 0, "result": [1]})
        if request.url.path.endswith("/sign/schedule"):
            calls["schedule"] += 1
            if calls["schedule"] == 2:
                reached.set()
                await release.wait()
            lesson = {"id": 1, "date": starts, "section_name": "Бег", "room_name": "Зал",
                      "section_level": 1, "type_id": 2, "available": 1, "signed": False,
                      "can_sign_in": {"can_sign_in": True}}
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [lesson]}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        return httpx.Response(200, json={"error_code": 0, "result": {"free_attempts": 1, "can_sign_in": True}})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        store.data["queues"]["owner"] = [{"lesson_id": 1, "section_name": "Бег", "starts_at": starts, "status": "waiting", "refusal_code": None}]
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        app = KronbotApp(store, sport=client, now=lambda: now, service_active=True)
        app.sport.sport_types = AsyncMock(return_value=[])
        app.sport.account_isu = AsyncMock(return_value=None)
        app.agent.bookings = AsyncMock(return_value=[])
        async with app.run_test(size=(80, 24)) as pilot:
            await asyncio.wait_for(reached.wait(), timeout=3)
            app.action_pause()
            assert store.data["background_paused"]
            release.set()
            await app.busy
            assert calls["posts"] == 0
            assert app.agent.queue()[0]["status"] == "waiting"
        await client.close()


async def test_running_service_respects_persisted_pause_on_open(tmp_path):
    from unittest.mock import AsyncMock
    from textual.widgets import Static

    now = 1900000000
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        store.data["background_paused"] = True
        app = KronbotApp(store, now=lambda: now, service_active=True)
        app.sport.sport_types = AsyncMock(return_value=[])
        app.sport.account_isu = AsyncMock(return_value=None)
        app.agent.check = AsyncMock()
        app.agent.bookings = AsyncMock(return_value=[])
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert not app.auto and not app.agent.booking_enabled
            assert "на паузе" in str(app.query_one("#error", Static).render())
            assert all(not call.kwargs["allow_booking"] for call in app.agent.check.await_args_list)
        await app.sport.close()
