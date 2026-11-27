"""Терминальный интерфейс kronbot."""

import asyncio
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Select, Static, TabbedContent, TabPane

from .agent import Agent, MSK, QueueError
from .agent import MSK, Agent, QueueError, auto_delay, opening_wait
from .auth import AuthError, ItmoAuth, LoginRequired, Restricted
from .browser import BrowserError
from .sport import LocalLimit, NetworkBlocked, SportClient, SportError, clean_text
from .state import StateError, StateLocked, StateStore

STATUSES = {"waiting": "Ожидает", "uncertain": "Проверьте в My ITMO", "booked": "Записан", "rejected": "Отказ", "expired": "Истекло"}



class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "reject", "Нет")]
    def __init__(self, message):
        super().__init__()
        self.message = message
    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(self.message, markup=False)
            with Horizontal():
                yield Button("Да", id="yes")
                yield Button("Нет", id="no", variant="primary")
    def on_mount(self):
        self.query_one("#no", Button).focus()
    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "yes")
    def action_reject(self):
        self.dismiss(False)


LOGIN_GUIDE = {
    "browser": (
        "1. Выйдите из TUI и выполните: kronbot browser install\n"
        "2. Нужны графическая сессия и работающий sandbox.\n"
        "3. Вернитесь в TUI, нажмите «Войти» и войдите в отдельном Chromium."
    ),
    "password": (
        "1. Введите логин ИТМО и пароль ниже.\n"
        "2. Пароль скрыт при вводе; нажмите Enter или «Войти».\n"
        "3. Если нужен дополнительный шаг, используйте веб-вход."
    ),
    "refresh": (
        "1. Вставьте только свой refresh-токен My ITMO в скрытое поле.\n"
        "2. Нажмите Enter или «Войти»: токен будет обменян и ротирован.\n"
        "Исходная сессия может перестать работать; не делите токен."
    ),
    "access": (
        "1. Вставьте только свой access-токен My ITMO в скрытое поле.\n"
        "2. Нажмите Enter или «Войти».\n"
        "Access-токен не продлевается автоматически; затем нужен новый вход."
    ),
}


class LoginScreen(ModalScreen):
    BINDINGS = [("escape", "back", "Назад")]
    def __init__(self):
        super().__init__()
        self.cancel = asyncio.Event()
        self.login_task = None
    def compose(self):
        with Vertical(classes="dialog login-dialog"):
            yield Label("Вход в ИТМО")
            notice = getattr(self.app, "service_notice", "")
            if notice:
                yield Static(notice, id="service-notice", markup=False)
            yield Select([("Веб-вход", "browser"), ("Логин и пароль", "password"), ("Refresh-токен My ITMO", "refresh"), ("Access-токен My ITMO", "access")], value="browser", allow_blank=False, id="method")
            yield Static("", id="login-guide", markup=False)
            yield Input(placeholder="Логин", id="username")
            yield Input(placeholder="Пароль или токен", password=True, id="secret")
            yield Static("", id="login-error", markup=False)
            with Horizontal():
                yield Button("Войти", id="submit", variant="primary")
                yield Button("Назад", id="back")
    def on_mount(self):
        self._fields()
    def on_select_changed(self, event):
        self._fields()
    def _fields(self):
        method = self.query_one("#method", Select).value
        self.query_one("#username", Input).display = method == "password"
        self.query_one("#secret", Input).display = method != "browser"
        self.query_one("#login-guide", Static).update(LOGIN_GUIDE[method])
    def on_input_submitted(self, event):
        self._start()
    def on_button_pressed(self, event):
        if event.button.id == "back":
            self.action_back()
        else:
            self._start()
    def _start(self):
        if self.login_task and not self.login_task.done():
            return
        method = self.query_one("#method", Select).value
        username = self.query_one("#username", Input).value
        secret_input = self.query_one("#secret", Input)
        secret = secret_input.value
        secret_input.value = ""
        self.query_one("#username", Input).value = ""
        self.login_task = asyncio.create_task(self._login(method, username, secret))
    async def _login(self, method, username, secret):
        app = self.app
        try:
            app.sport.check_blocks()
            auth = ItmoAuth(on_restricted=app.sport._auth_restricted)
            if method == "browser":
                session = await auth.login_browser(self.cancel)
            elif method == "password":
                session = await auth.login_password(username, secret)
            elif method == "refresh":
                session = await auth.import_refresh(secret)
            else:
                session = await auth.import_access(secret)
            if self.cancel.is_set():
                return
            old = app.store.data["session"]
            app.store.data["session"] = asdict(session)
            try:
                app.store.save()
            except StateError:
                app.store.data["session"] = old
                raise
            self.dismiss(True)
        except (AuthError, BrowserError, SportError, StateError) as exc:
            self.query_one("#login-error", Static).update(str(exc))
        finally:
            username = secret = ""
    def on_unmount(self):
        for field in self.query(Input):
            field.value = ""
    def action_back(self):
        self.cancel.set()
        self.query_one("#username", Input).value = ""
        self.query_one("#secret", Input).value = ""
        if self.login_task and not self.login_task.done():
            asyncio.create_task(self._close_after_task())
        else:
            self.dismiss(False)
    async def _close_after_task(self):
        await self.login_task
        if self.is_attached:
            self.dismiss(False)


class KronbotApp(App):
    CSS = """
    Screen { layout: vertical; }
    .dialog { width: 75%; max-width: 80; height: auto; max-height: 95%; padding: 1 2; border: solid $accent; background: $surface; align: center middle; }
    .login-dialog { padding: 0 1; }
    #login-guide { height: auto; }
    ModalScreen { align: center middle; }
    .dialog Horizontal { height: auto; align: center middle; }
    .dialog Button { margin: 0 1; }
    #status { height: 3; }
    #error { height: 2; }
    TabbedContent { height: 1fr; }
    DataTable { height: 1fr; }
    #filter-search-row, #filter-options { height: 3; }
    #filter-search { width: 1fr; }
    #filter-type { width: 18; }
    #filter-options Select { width: 1fr; }
    #filter-clear { width: 14; min-width: 14; }
    #account { height: auto; padding: 1; }
    #account-actions { height: 3; }
    """
    BINDINGS = [
        ("p", "pause", "Авто"),
        ("/", "search", "Поиск"),
        ("r", "refresh", "Обновить"),
        ("x", "remove", "Удалить"),
        ("l", "login", "Вход"),
        ("u", "logout", "Забыть вход"),
        ("q", "quit_safe", "Выход"),
        Binding("ctrl+c", "quit_safe", "Выход", priority=True),
    ]

    def __init__(
        self,
        store,
        *,
        sport=None,
        now=time.time,
        monotonic=time.monotonic,
        service_notice="",
    ):
        super().__init__()
        self.store = store
        self.sport = sport or SportClient(store, now=now)
        self.agent = Agent(store, self.sport, now=now, monotonic=monotonic)
        self.now, self.monotonic = now, monotonic
        self.auto = False
        self.busy = None
        self.last_auto = None
        self.auto_interval = 120.0
        self.limit_until = 0
        self.error = ""
        self.service_notice = service_notice
        self.stale = False
        self.bookings_snapshot = []
        self.bookings_stale = True
        self.bookings_owner = None
        self.isu_checked = False
        self.filter_type = "all"
        self.filter_ids = None
        self.filter_stale = False
        self.filter_generation = 0
        self.filter_task = None
        self.schedule_selection = None
        self.hidden_schedule_selection = None
        self.stopping = False

    def compose(self):
        yield Header()
        yield Static("", id="status", markup=False)
        yield Static("", id="error", markup=False)
        with TabbedContent():
            with TabPane("Расписание", id="schedule-tab"):
                with Horizontal(id="filter-search-row"):
                    yield Input(placeholder="Поиск секции или зала (/)", id="filter-search")
                    yield Select([("Все виды спорта", "all")], value="all", allow_blank=False, id="filter-type")
                with Horizontal(id="filter-options"):
                    yield Select([("Запись: любая", "all"), ("Запись: да", "yes"), ("Запись: нет", "no")], value="all", allow_blank=False, id="filter-sign")
                    yield Select([("Места: любые", "all"), ("Места: есть", "yes"), ("Места: нет", "no")], value="all", allow_blank=False, id="filter-places")
                    yield Select([("Время: любое", "all"), ("До 12:00", "morning"), ("12:00–17:59", "day"), ("С 18:00", "evening")], value="all", allow_blank=False, id="filter-time")
                    yield Button("Сброс", id="filter-clear")
                yield DataTable(id="schedule", cursor_type="row")
            with TabPane("Очередь", id="queue-tab"):
                yield DataTable(id="queue", cursor_type="row")
            with TabPane("Записи", id="bookings-tab"):
                yield DataTable(id="bookings", cursor_type="row")
            with TabPane("Аккаунт", id="account-tab"):
                yield Static("", id="account", markup=False)
                with Horizontal(id="account-actions"):
                    yield Button("Вход", id="account-login")
                    yield Button("Забыть вход", id="account-logout")
        yield Footer()

    def on_mount(self):
        self.query_one("#schedule", DataTable).add_columns("ID", "МСК", "Секция", "Зал", "Места", "Доступность", "Очередь")
        self.query_one("#queue", DataTable).add_columns("ID", "МСК", "Секция", "Статус")
        self.query_one("#bookings", DataTable).add_columns("ID", "МСК", "Секция", "Зал")
        self._render()
        self.set_interval(0.5, self._tick)
        if self.store.data["session"] is None:
            self.action_login()
        else:
            self.action_refresh()

    def _render(self):
        session = self.store.data["session"]
        owner = session["sub"] if session else None
        if owner != self.bookings_owner:
            self.bookings_snapshot = []
            self.bookings_stale = True
            self.bookings_owner = owner
        expiry = ""
        if session:
            expiry = " | access: " + datetime.fromtimestamp(session["access_expires_at"], MSK).strftime("%d.%m %H:%M")
            if session["refresh_expires_at"]:
                expiry += " | refresh: " + datetime.fromtimestamp(
                    session["refresh_expires_at"], MSK
                ).strftime("%d.%m %H:%M")
        checked = (
            datetime.fromtimestamp(self.agent.last_check, MSK).strftime("%d.%m %H:%M")
            if self.agent.last_check
            else "—"
        )
        self.query_one("#status", Static).update(
            f"ИТМО: {'вход выполнен' if session else 'нужен вход'}{expiry}\nАвтозапись: {'включена' if self.auto else 'на паузе'} | Последняя проверка: {checked}"
        )
        isu = clean_text(str(session.get("isu") or "")) if session else ""
        self.query_one("#account", Static).update(
            f"Состояние: {'вход выполнен' if session else 'нужен вход'}\n"
            f"Номер ИСУ: {isu or 'не указан'}\n"
            f"Срок сессии (МСК):{expiry or ' не указан'}\n"
            f"Автозапись: {'включена' if self.auto else 'на паузе'}"
        )
        stale = (
            self.bookings_stale
            if self.query_one(TabbedContent).active == "bookings-tab"
            else self.stale or self.filter_stale
        )
        messages = [
            ("Данные устарели. " if stale else "") + self.error,
            self.service_notice,
        ]
        self.query_one("#error", Static).update(
            "\n".join(message for message in messages if message)
        )
        queue = self.agent.queue() if session else []
        table = self.query_one("#schedule", DataTable)
        selected = self.hidden_schedule_selection or self._selected(table) or self.schedule_selection
        table.clear()
        visible = [lesson for lesson in self.agent.snapshot if self._matches_filters(lesson)]
        visible_ids = {str(lesson.id) for lesson in visible}
        self.hidden_schedule_selection = selected if selected and selected not in visible_ids else None
        for lesson in visible:
            queued = next((x for x in queue if x["lesson_id"] == lesson.id), None)
            availability = "Да" if lesson.can_sign_in else "Нет"
            if lesson.intersection:
                availability += "; Пересечение с расписанием"
            table.add_row(
                Text(str(lesson.id)),
                Text(lesson.starts_at.astimezone(MSK).strftime("%d.%m %H:%M")),
                Text(clean_text(lesson.section_name)),
                Text(clean_text(lesson.room_name)),
                Text(str(lesson.available)),
                Text(availability),
                Text(STATUSES[queued["status"]] if queued else ""),
                key=str(lesson.id),
            )
        if not visible:
            table.add_row(
                Text(""),
                Text("Нет занятий на две недели вперёд" if not self.agent.snapshot else "Нет занятий по фильтрам"),
                Text(""),
                Text(""),
                Text(""),
                Text(""),
                Text(""),
            )
        self._restore(table, selected)
        self.schedule_selection = selected
        table = self.query_one("#queue", DataTable)
        selected = self._selected(table)
        table.clear()
        for item in queue:
            status = STATUSES[item["status"]]
            if item["refusal_code"] is not None:
                status += f" ({item['refusal_code']})"
            table.add_row(Text(str(item["lesson_id"])), Text(datetime.fromisoformat(item["starts_at"]).astimezone(MSK).strftime("%d.%m %H:%M")), Text(item["section_name"]), Text(status), key=str(item["lesson_id"]))
        if not queue:
            table.add_row(Text(""), Text("Очередь пуста"), Text(""), Text(""))
        self._restore(table, selected)
        table = self.query_one("#bookings", DataTable)
        selected = self._selected(table)
        table.clear()
        for booking in self.bookings_snapshot:
            table.add_row(
                Text(str(booking.id)),
                Text(booking.starts_at.astimezone(MSK).strftime("%d.%m %H:%M")),
                Text(clean_text(booking.section_name)),
                Text(clean_text(booking.room_name)),
                key=str(booking.id),
            )
        if not self.bookings_snapshot:
            table.add_row(Text(""), Text("Нет активных записей"), Text(""), Text(""))
        self._restore(table, selected)

    def _matches_filters(self, lesson):
        search = self.query_one("#filter-search", Input).value.strip().casefold()
        sport_type = self.query_one("#filter-type", Select).value
        sign = self.query_one("#filter-sign", Select).value
        places = self.query_one("#filter-places", Select).value
        period = self.query_one("#filter-time", Select).value
        hour = lesson.starts_at.astimezone(MSK).hour
        return (
            (not search or search in clean_text(lesson.section_name).casefold()
             or search in clean_text(lesson.room_name).casefold())
            and (sport_type == "all" or self.filter_ids is not None and lesson.id in self.filter_ids)
            and (sign == "all" or (sign == "yes") == lesson.can_sign_in)
            and (places == "all" or (places == "yes") == (lesson.available > 0))
            and (period == "all" or period == "morning" and hour < 12
                 or period == "day" and 12 <= hour < 18
                 or period == "evening" and hour >= 18)
        )

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "filter-search" and self.is_mounted:
            self._render()

    def on_select_changed(self, event: Select.Changed):
        if not self.is_mounted:
            return
        if event.select.id == "filter-type":
            selected = event.select.value
            if selected != self.filter_type:
                self.filter_type = selected
                self.filter_generation += 1
                self.filter_ids = None
                self.filter_stale = selected != "all"
                self._render()
                if selected != "all":
                    self._start_filter()
        elif event.select.id in ("filter-sign", "filter-places", "filter-time"):
            self._render()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "filter-clear":
            self.query_one("#filter-search", Input).value = ""
            for selector in ("#filter-type", "#filter-sign", "#filter-places", "#filter-time"):
                self.query_one(selector, Select).value = "all"
            self._render()
        elif event.button.id == "account-login":
            self.action_login()
        elif event.button.id == "account-logout":
            self.action_logout()

    def action_search(self):
        if self.query_one(TabbedContent).active == "schedule-tab":
            self.query_one("#filter-search", Input).focus()

    def _start_filter(self):
        if self.stopping or not self.store.data["session"]:
            return
        if self.filter_task and not self.filter_task.done():
            return
        previous = self.busy
        self.filter_task = asyncio.create_task(self._filter_schedule(previous))
        self.busy = self.filter_task

    async def _filter_schedule(self, previous):
        if previous and not previous.done():
            await previous
        while not self.stopping and self.store.data["session"] and self.filter_type != "all":
            generation = self.filter_generation
            selected = self.filter_type
            owner = self.store.data["session"]["sub"]
            today = datetime.fromtimestamp(self.now(), MSK).date()
            try:
                lessons = await self.sport.schedule(today, today + timedelta(days=14), sport_type_id=selected)
            except (AuthError, SportError, StateError) as exc:
                if isinstance(exc, LocalLimit):
                    self.limit_until = exc.until
                if isinstance(exc, (Restricted, LoginRequired, NetworkBlocked)):
                    self.auto = False
                    self.agent.booking_enabled = False
                if generation == self.filter_generation or isinstance(exc, (LocalLimit, Restricted, LoginRequired, NetworkBlocked)):
                    self.filter_stale = self.filter_type != "all"
                    self.error = str(exc)
                    self._render()
                    return
            else:
                if generation == self.filter_generation and self.store.data["session"] and self.store.data["session"]["sub"] == owner:
                    self.filter_ids = {lesson.id for lesson in lessons}
                    self.filter_stale = False
                    self.error = ""
                    self._render()
                    return

    async def _refresh_sport_types(self):
        types = await self.sport.sport_types()
        select = self.query_one("#filter-type", Select)
        current = self.filter_type
        valid = {ident for ident, _ in types}
        select.set_options([("Все виды спорта", "all")] + [(Text(clean_text(name)), ident) for ident, name in types])
        select.value = current if current in valid else "all"

    def _reset_sport_filter(self):
        self.filter_type = "all"
        self.filter_ids = None
        self.filter_stale = False
        self.filter_generation += 1
        select = self.query_one("#filter-type", Select)
        select.set_options([("Все виды спорта", "all")])
        select.value = "all"

    def on_tabbed_content_tab_activated(self, event):
        self._render()

    @staticmethod
    def _selected(table):
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None
    @staticmethod
    def _restore(table, key):
        if key:
            try:
                table.move_cursor(row=table.get_row_index(key))
            except Exception:
                pass

    def _start(self, auto=False, bookings_only=False):
        if self.busy and not self.busy.done() or self.stopping:
            return
        self.busy = asyncio.create_task(self._check(auto, bookings_only))
    async def _check(self, auto, bookings_only=False):
        schedule_updated = False
        if not auto:
            self.bookings_stale = True
        try:
            if not bookings_only:
                await self.agent.check(allow_booking=auto)
                self.stale = False
                schedule_updated = True
            self.error = ""
            if not auto or self.agent.confirmed_ids:
                self.bookings_stale = True
                self.bookings_snapshot = await self.agent.bookings()
                self.bookings_owner = self.store.data["session"]["sub"]
                self.bookings_stale = False
        except (AuthError, SportError, StateError, QueueError) as exc:
            self.error = str(exc)
            if not bookings_only:
                self.stale = True
            if isinstance(exc, LocalLimit):
                self.limit_until = exc.until
            if isinstance(exc, (Restricted, LoginRequired, NetworkBlocked)):
                self.auto = False
                self.agent.booking_enabled = False
        finally:
            if not auto and not self.stopping and self.store.data["session"]:
                try:
                    await self._refresh_sport_types()
                except (AuthError, SportError, StateError) as exc:
                    self.error = str(exc)
                    if isinstance(exc, LocalLimit):
                        self.limit_until = exc.until
                    if isinstance(exc, (Restricted, LoginRequired, NetworkBlocked)):
                        self.auto = False
                        self.agent.booking_enabled = False
            session = self.store.data["session"]
            if not auto and not self.stopping and session and not session.get("isu") and not self.isu_checked:
                self.isu_checked = True
                try:
                    await self.sport.account_isu()
                except (AuthError, SportError, StateError) as exc:
                    if isinstance(exc, LocalLimit):
                        self.limit_until = exc.until
                    if isinstance(exc, (Restricted, LoginRequired, NetworkBlocked)):
                        self.auto = False
                        self.agent.booking_enabled = False
                        self.error = str(exc)
            if auto:
                self.auto_interval = auto_delay(self.now())
                self.last_auto = self.monotonic()
                if self.store.data["session"] and not any(x["status"] == "waiting" for x in self.agent.queue()):
                    self.auto = False
                    self.agent.booking_enabled = False
            if schedule_updated and self.filter_type != "all" and not self.stopping:
                self.filter_generation += 1
                self.filter_stale = True
                self._start_filter()
            self._render()

    def _tick(self):
        if not self.auto or self.busy and not self.busy.done() or not self.store.data["session"]:
            return
        if self.now() < self.limit_until:
            return
        queue = self.agent.queue()
        if not any(x["status"] in ("waiting", "uncertain") for x in queue):
            return
        if self.last_auto is None and any(x["status"] == "waiting" for x in queue) and opening_wait(self.now()):
            return
        if self.last_auto is None or self.monotonic() - self.last_auto >= self.auto_interval:
            self._start(True)

    def action_refresh(self):
        self._start(
            False, bookings_only=self.query_one(TabbedContent).active == "bookings-tab"
        )

    def action_pause(self):
        self.auto = not self.auto
        self.agent.booking_enabled = self.auto
        self._render()
        self._tick()
    def action_login(self):
        self.auto = False
        self.agent.booking_enabled = False
        self.agent.cancel_enabled = False
        async def open_login():
            if self.busy and not self.busy.done():
                await self.busy
            self.push_screen(LoginScreen(), self._login_result)
        asyncio.create_task(open_login())
    def _login_result(self, logged_in):
        self.agent.cancel_enabled = True
        if logged_in:
            self._reset_sport_filter()
            self.isu_checked = False
            self.bookings_snapshot = []
            self.bookings_stale = True
            self.bookings_owner = None
            self.agent.snapshot = []
        self._render()
        if logged_in:
            self.action_refresh()
        elif self.store.data["session"] is None:
            asyncio.create_task(self.action_quit_safe())
    def action_logout(self):
        self.auto = False
        self.agent.booking_enabled = False
        self.agent.cancel_enabled = False
        self.push_screen(ConfirmScreen("Забыть локальный вход? Серверная сессия останется активной."), self._logout_result)
    def _logout_result(self, confirmed):
        if confirmed:
            asyncio.create_task(self._logout_after_work())
        else:
            self.agent.cancel_enabled = True

    async def _logout_after_work(self):
        if self.busy and not self.busy.done():
            await self.busy
        self.store.data["session"] = None
        try:
            self.store.save()
        except StateError as exc:
            self.error = str(exc)
        self.bookings_snapshot = []
        self.bookings_stale = True
        self.bookings_owner = None
        self._reset_sport_filter()
        self.agent.snapshot = []
        self.agent.cancel_enabled = True
        self._render()
    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        if event.data_table.id == "schedule" and event.row_key.value:
            self.schedule_selection = event.row_key.value
            self.hidden_schedule_selection = None
        key = event.row_key.value
        if not key:
            return
        if event.data_table.id == "schedule":
            try:
                lesson = next((x for x in self.agent.snapshot if x.id == int(key) and self._matches_filters(x)), None)
                if lesson is None:
                    return
                if self.stale or self.filter_stale:
                    raise QueueError("Расписание устарело. Обновите его.")
                self.agent.add(lesson)
                self.error = ""
            except (QueueError, LoginRequired, StateError) as exc:
                self.error = str(exc)
            self._render()
            self._tick()
        elif event.data_table.id == "bookings":
            self._confirm_cancel(key)
        elif event.data_table.id == "queue":
            item = next((x for x in self.agent.queue() if x["lesson_id"] == int(key)), None)
            if item and item["status"] == "uncertain":
                self.push_screen(ConfirmScreen("Проверьте запись в My ITMO. Если записи нет, разрешить новую попытку?"), lambda yes: self._retry(int(key), yes))
    def _retry(self, key, yes):
        if yes:
            try:
                self.agent.retry(key, verified=True)
            except (QueueError, StateError) as exc:
                self.error = str(exc)
            self._render()
    def action_remove(self):
        if self.query_one(TabbedContent).active == "bookings-tab":
            self._confirm_cancel(self._selected(self.query_one("#bookings", DataTable)))
            return
        if self.query_one(TabbedContent).active != "queue-tab" or not self.store.data["session"]:
            return
        key = self._selected(self.query_one("#queue", DataTable))
        if not key:
            return
        item = next((x for x in self.agent.queue() if x["lesson_id"] == int(key)), None)
        if item and item["status"] == "uncertain":
            self.push_screen(ConfirmScreen("Проверьте запись в My ITMO. Удалить локальную запись с неизвестным исходом?"), lambda yes: self._remove(int(key), yes))
        else:
            self._remove(int(key), True)
    def _remove(self, key, yes):
        if yes:
            try:
                self.agent.remove(key, verified=True)
            except (QueueError, StateError) as exc:
                self.error = str(exc)
            self._render()

    def _confirm_cancel(self, key):
        if not key or self.busy and not self.busy.done() or self.stopping:
            return
        if (
            self.bookings_stale
            or not self.store.data["session"]
            or self.bookings_owner != self.store.data["session"]["sub"]
        ):
            self.error = "Записи устарели. Обновите их."
            self._render()
            return
        booking = next(
            (item for item in self.bookings_snapshot if str(item.id) == key), None
        )
        if booking is None:
            return
        if booking.starts_at.timestamp() <= self.now():
            self.error = (
                "Занятие уже началось. Проверьте возможность отписки в My ITMO."
            )
            self._render()
            return
        if booking.section_level not in (1, 2, 3, 4):
            self.error = "Неизвестен тип записи. Отмените её в My ITMO."
            self._render()
            return
        if booking.section_level in (2, 3, 4) and (
            type(booking.lesson_group_id) is not int or booking.lesson_group_id <= 0
        ):
            self.error = "Неизвестен идентификатор группы. Отмените запись в My ITMO."
            self._render()
            return
        message = f"Отписаться от «{clean_text(booking.section_name)}» (ID {booking.id}) в My ITMO?"
        if booking.section_level in (2, 3, 4):
            message += " Будет отменена запись на всю группу занятий."
        self.push_screen(
            ConfirmScreen(message), lambda yes: self._cancel_confirmed(booking, yes)
        )

    def _cancel_confirmed(self, booking, yes):
        if not yes or self.stopping or self.busy and not self.busy.done():
            return
        if (
            self.bookings_stale
            or not self.store.data["session"]
            or self.bookings_owner != self.store.data["session"]["sub"]
            or booking not in self.bookings_snapshot
        ):
            return
        self.busy = asyncio.create_task(self._cancel_booking(booking))

    async def _cancel_booking(self, booking):
        self.bookings_stale = True
        try:
            await self.agent.cancel_booking(booking)
            self.bookings_snapshot = await self.agent.bookings()
            self.bookings_stale = False
            self.error = ""
        except (AuthError, SportError, StateError, QueueError) as exc:
            self.error = str(exc)
            if isinstance(exc, LocalLimit):
                self.limit_until = exc.until
            if isinstance(exc, (Restricted, LoginRequired, NetworkBlocked)):
                self.auto = False
                self.agent.booking_enabled = False
        finally:
            self._render()

    async def action_quit_safe(self):
        self.stopping = True
        self.auto = False
        self.agent.booking_enabled = False
        self.agent.cancel_enabled = False
        self.sport.stopped = True
        # A cancellation already in flight must finish before stopping the client.
        if isinstance(self.screen, LoginScreen):
            self.screen.cancel.set()
            if self.screen.login_task:
                await self.screen.login_task
        if self.busy and not self.busy.done():
            await self.busy
        await self.sport.close()
        self.exit()


def run():
    try:
        with StateStore() as store:
            from .cli import SERVICE_PAUSED_NOTICE, _service_notice

            notice = _service_notice(
                store.directory.expanduser().absolute(), install=True
            )
            if store.data["background_paused"]:
                notice = "\n".join(filter(None, (notice, SERVICE_PAUSED_NOTICE)))
            KronbotApp(store, service_notice=notice).run()
        return 0
    except StateLocked as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except StateError as exc:
        print(str(exc), file=sys.stderr)
        return 1
