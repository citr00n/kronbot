"""Терминальный интерфейс kronbot."""

import asyncio
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Select, Static, TabbedContent, TabPane

from .agent import Agent, MSK, QueueError
from .auth import AuthError, ItmoAuth, LoginRequired, Restricted
from .browser import BrowserError
from .sport import LocalLimit, NetworkBlocked, SportClient, SportError
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
    """
    BINDINGS = [("p", "pause", "Авто"), ("r", "refresh", "Обновить"), ("x", "remove", "Удалить"),
                ("l", "login", "Вход"), ("u", "logout", "Забыть вход"), ("q", "quit_safe", "Выход"),
                ("ctrl+c", "quit_safe", "Выход")]

    def __init__(self, store, *, sport=None, now=time.time, monotonic=time.monotonic):
        super().__init__()
        self.store = store
        self.sport = sport or SportClient(store, now=now)
        self.agent = Agent(store, self.sport, now=now, monotonic=monotonic)
        self.now, self.monotonic = now, monotonic
        self.auto = False
        self.busy = None
        self.last_auto = None
        self.limit_until = 0
        self.error = ""
        self.stale = False
        self.stopping = False

    def compose(self):
        yield Header()
        yield Static("", id="status", markup=False)
        yield Static("", id="error", markup=False)
        with TabbedContent():
            with TabPane("Расписание", id="schedule-tab"):
                yield DataTable(id="schedule", cursor_type="row")
            with TabPane("Очередь", id="queue-tab"):
                yield DataTable(id="queue", cursor_type="row")
        yield Footer()

    def on_mount(self):
        self.query_one("#schedule", DataTable).add_columns("ID", "МСК", "Секция", "Зал", "Места", "Доступность", "Очередь")
        self.query_one("#queue", DataTable).add_columns("ID", "МСК", "Секция", "Статус")
        self._render()
        self.set_interval(0.5, self._tick)
        if self.store.data["session"] is None:
            self.action_login()
        else:
            self.action_refresh()

    def _render(self):
        session = self.store.data["session"]
        expiry = ""
        if session:
            expiry = " | access: " + datetime.fromtimestamp(session["access_expires_at"], MSK).strftime("%d.%m %H:%M")
            if session["refresh_expires_at"]:
                expiry += " | refresh: " + datetime.fromtimestamp(session["refresh_expires_at"], MSK).strftime("%d.%m %H:%M")
        checked = datetime.fromtimestamp(self.agent.last_check, MSK).strftime("%d.%m %H:%M") if self.agent.last_check else "—"
        self.query_one("#status", Static).update(f"ИТМО: {'вход выполнен' if session else 'нужен вход'}{expiry}\nАвтозапись: {'включена' if self.auto else 'на паузе'} | Последняя проверка: {checked}")
        self.query_one("#error", Static).update(("Данные устарели. " if self.stale else "") + self.error)
        queue = self.agent.queue() if session else []
        table = self.query_one("#schedule", DataTable)
        selected = self._selected(table)
        table.clear()
        for lesson in self.agent.snapshot:
            queued = next((x for x in queue if x["lesson_id"] == lesson.id), None)
            availability = "Да" if lesson.can_sign_in else "Нет"
            if lesson.intersection:
                availability += "; Пересечение с расписанием"
            table.add_row(Text(str(lesson.id)), Text(lesson.starts_at.astimezone(MSK).strftime("%d.%m %H:%M")), Text(lesson.section_name), Text(lesson.room_name), Text(str(lesson.available)), Text(availability), Text(STATUSES[queued["status"]] if queued else ""), key=str(lesson.id))
        if not self.agent.snapshot:
            table.add_row(Text(""), Text("Нет занятий на ближайшие 7 дней"), Text(""), Text(""), Text(""), Text(""), Text(""))
        self._restore(table, selected)
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

    def _start(self, auto=False):
        if self.busy and not self.busy.done() or self.stopping:
            return
        self.busy = asyncio.create_task(self._check(auto))
    async def _check(self, auto):
        try:
            await self.agent.check(allow_booking=auto)
            self.error = ""
            self.stale = False
        except (AuthError, SportError, StateError, QueueError) as exc:
            self.error = str(exc)
            self.stale = True
            if isinstance(exc, LocalLimit):
                self.limit_until = exc.until
            if isinstance(exc, (Restricted, LoginRequired, NetworkBlocked)):
                self.auto = False
                self.agent.booking_enabled = False
        finally:
            if auto:
                self.last_auto = self.monotonic()
                if self.store.data["session"] and not any(x["status"] == "waiting" for x in self.agent.queue()):
                    self.auto = False
                    self.agent.booking_enabled = False
            self._render()

    def _tick(self):
        if not self.auto or self.busy and not self.busy.done() or not self.store.data["session"]:
            return
        if self.now() < self.limit_until:
            return
        queue = self.agent.queue()
        if not any(x["status"] in ("waiting", "uncertain") for x in queue):
            return
        if self.last_auto is None or self.monotonic() - self.last_auto >= 120:
            self._start(True)

    def action_refresh(self):
        self._start(False)
    def action_pause(self):
        self.auto = not self.auto
        self.agent.booking_enabled = self.auto
        self._render()
        self._tick()
    def action_login(self):
        self.auto = False
        self.agent.booking_enabled = False
        async def open_login():
            if self.busy and not self.busy.done():
                await self.busy
            self.push_screen(LoginScreen(), self._login_result)
        asyncio.create_task(open_login())
    def _login_result(self, logged_in):
        self._render()
        if logged_in:
            self.action_refresh()
        elif self.store.data["session"] is None:
            asyncio.create_task(self.action_quit_safe())
    def action_logout(self):
        self.auto = False
        self.agent.booking_enabled = False
        self.push_screen(ConfirmScreen("Забыть локальный вход? Серверная сессия останется активной."), self._logout_result)
    def _logout_result(self, confirmed):
        if confirmed:
            asyncio.create_task(self._logout_after_work())

    async def _logout_after_work(self):
        if self.busy and not self.busy.done():
            await self.busy
        self.store.data["session"] = None
        try:
            self.store.save()
        except StateError as exc:
            self.error = str(exc)
        self.agent.snapshot = []
        self._render()
    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        key = event.row_key.value
        if not key:
            return
        if event.data_table.id == "schedule":
            try:
                lesson = next(x for x in self.agent.snapshot if x.id == int(key))
                if self.stale:
                    raise QueueError("Расписание устарело. Обновите его.")
                self.agent.add(lesson)
                self.error = ""
            except (QueueError, LoginRequired, StateError) as exc:
                self.error = str(exc)
            self._render()
            self._tick()
        else:
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
    async def action_quit_safe(self):
        self.stopping = True
        self.auto = False
        self.agent.booking_enabled = False
        self.sport.stopped = True
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
            KronbotApp(store).run()
        return 0
    except StateLocked as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except StateError as exc:
        print(str(exc), file=sys.stderr)
        return 1
