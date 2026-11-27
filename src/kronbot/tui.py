"""Терминальный интерфейс kronbot."""

import asyncio
import calendar
import sys
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Select, Static, TabbedContent, TabPane

from .agent import MSK, Agent, QueueError, auto_delay, opening_wait, opening_window
from .auth import AuthError, ItmoAuth, LoginRequired, Restricted
from .browser import BrowserError
from .login_guides import LOGIN_GUIDE
from .notifications import validate_url
from .sport import LocalLimit, NetworkBlocked, SportClient, SportError, clean_text, forecast_prototypes
from .state import StateError, StateLocked, StateStore
from .updates import check_update

STATUSES = {"waiting": "Ожидает", "uncertain": "Проверьте в My ITMO", "booked": "Записан", "rejected": "Отказ", "expired": "Истекло"}

FORECAST_STATUSES = {
    "not_checked": "Ещё не проверен",
    "not_open": "Запись ещё не открыта",
    "no_match": "Занятие не найдено",
    "ambiguous": "Несколько совпадений; выбор невозможен",
    "incomplete": "Недостаточно данных для сопоставления",
    "matched": "Занятие найдено",
}



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

class ForceLimitsScreen(ConfirmScreen):
    def __init__(self):
        super().__init__(
            "ВНИМАНИЕ: повышенная частота запросов может привести к ограничению доступа "
            "или другим последствиям для аккаунта. Локальный лимит 60 спортивных запросов "
            "в час отключается, повторная автоматическая проверка — каждые 10 секунд вместо 120. "
            "Настройка также действует на фоновую службу. Серверные блокировки 403/429, "
            "авторизация, проверка попыток и защита неопределённого исхода остаются обязательными. "
            "Включить принудительный режим?"
        )

    def compose(self):
        with Vertical(classes="dialog force-dialog"):
            yield Static(self.message, id="force-disclaimer", markup=False)
            with Horizontal():
                yield Button("Да", id="yes", variant="error")
                yield Button("Нет", id="no", variant="primary")






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
            store = getattr(self.app, "store", None)
            if store is not None and store.data["background_paused"]:
                notice = "\n".join(filter(None, (
                    notice, "Фоновая автозапись на паузе; возобновите её клавишей p после окончания блокировки."
                )))
            if notice:
                yield Static(notice, id="service-notice", markup=False)
            update_notice = Static(getattr(self.app, "update_notice", ""), id="login-update", markup=False)
            update_notice.display = bool(getattr(self.app, "update_notice", ""))
            yield update_notice
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
        notice = getattr(self.app, "update_notice", "")
        if notice:
            widget = self.query_one("#login-update", Static)
            widget.update(notice)
            widget.display = True
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
            if isinstance(exc, (Restricted, NetworkBlocked)):
                app._stop_for_error(exc)
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


class NtfyScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "back", "Назад")]

    def compose(self):
        settings = self.app.store.data["ntfy"]
        with Vertical(classes="dialog"):
            yield Label("Уведомления ntfy")
            yield Static("HTTPS-адрес топика и необязательный bearer-токен вводятся скрыто.", markup=False)
            yield Input(value=settings["url"] if settings else "", placeholder="HTTPS URL топика", password=True, id="ntfy-url")
            yield Input(value=(settings["token"] or "") if settings else "", placeholder="Bearer-токен (необязательно)", password=True, id="ntfy-token")
            yield Static("", id="ntfy-error", markup=False)
            with Horizontal():
                yield Button("Сохранить", id="ntfy-save", variant="primary")
                yield Button("Отключить", id="ntfy-disable")
                yield Button("Назад", id="ntfy-back")

    def on_input_submitted(self, event):
        if event.input.id == "ntfy-url":
            self.query_one("#ntfy-token", Input).focus()
        else:
            self._save()

    def on_button_pressed(self, event):
        if event.button.id == "ntfy-back":
            self.action_back()
        elif event.button.id == "ntfy-disable":
            self._save(disable=True)
        elif event.button.id == "ntfy-save":
            self._save()

    def _save(self, *, disable=False):
        if disable:
            settings = None
        else:
            try:
                url = validate_url(self.query_one("#ntfy-url", Input).value)
            except ValueError:
                self.query_one("#ntfy-error", Static).update("Некорректный HTTPS URL топика ntfy.")
                return
            token = self.query_one("#ntfy-token", Input).value or None
            settings = {"url": url, "token": token, "last_error": None}
        store = self.app.store
        old = store.data["ntfy"]
        store.data["ntfy"] = settings
        try:
            store.save()
        except StateError:
            store.data["ntfy"] = old
            self.query_one("#ntfy-error", Static).update("Не удалось сохранить настройки ntfy.")
            return
        self.dismiss(True)

    def on_unmount(self):
        for field in self.query(Input):
            field.value = ""

    def action_back(self):
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
    .force-dialog { border: solid red; }
    #force-disclaimer, #force-warning { color: red; text-style: bold; }
    #force-warning { height: 1; }
    #opening-warning { color: red; text-style: bold; height: 1; }
    #status { height: 3; }
    #update-notice, #login-update { height: auto; }
    #error { height: 2; }
    TabbedContent { height: 1fr; }
    DataTable { height: 1fr; }
    #filter-search-row, #filter-options { height: 3; }
    #filter-search { width: 1fr; }
    #filter-type { width: 18; }
    #filter-options Select { width: 1fr; }
    #filter-clear { width: 14; min-width: 14; }
    #date-filter { height: 1; }
    #calendar-controls { height: 3; }
    #calendar-month { width: 1fr; content-align: center middle; }
    #calendar-legend { height: auto; }
    #calendar { height: 1fr; }
    #account { height: auto; padding: 1; }
    #account-actions { height: 3; }
    """
    BINDINGS = [
        ("p", "pause", "Авто"),
        ("/", "search", "Поиск"),
        ("r", "refresh", "Обновить"),
        ("x", "remove", "Удалить"),
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
        service_active=False,
        service_probe=None,
    ):
        super().__init__()
        self.store = store
        self.sport = sport or SportClient(store, now=now)
        self.agent = Agent(store, self.sport, now=now, monotonic=monotonic)
        self.now, self.monotonic = now, monotonic
        self.service_active = service_active
        self.service_probe = service_probe
        self.service_probe_task = None
        self.next_service_probe = 0.0
        self.suspended = False
        self.resume_after_dialog = False
        self.seen_uncertain = set()
        self.auto = bool(service_active and not store.data["background_paused"] and store.data["session"])
        self.agent.booking_enabled = self.auto
        self.busy = None
        self.last_auto = None
        self.auto_interval = 120.0
        self.limit_until = 0
        self.error = ""
        self.service_notice = service_notice
        self.update_notice = ""
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
        self.selected_date = None
        today = datetime.fromtimestamp(now(), MSK).date()
        self.calendar_month = today.replace(day=1)
        self.calendar_days = []
        self.opening_active = False
        self.stopping = False

    def compose(self):
        yield Header()
        yield Static("", id="status", markup=False)
        force_warning = Static("ВНИМАНИЕ: локальные лимиты отключены; автоинтервал 10 с", id="force-warning", markup=False)
        force_warning.display = False
        yield force_warning
        opening_warning = Static("ОКНО ОТКРЫТИЯ: опрос каждые 10 с; лимит 60 запросов/час отключён", id="opening-warning", markup=False)
        opening_warning.display = False
        yield opening_warning
        yield Static("", id="error", markup=False)
        update_notice = Static("", id="update-notice", markup=False)
        update_notice.display = False
        yield update_notice
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
                yield Static("", id="date-filter", markup=False)
                yield DataTable(id="schedule", cursor_type="row")
            with TabPane("Календарь", id="calendar-tab"):
                with Horizontal(id="calendar-controls"):
                    yield Button("◀", id="calendar-prev")
                    yield Static("", id="calendar-month", markup=False)
                    yield Button("▶", id="calendar-next")
                    yield Button("Все даты", id="calendar-clear")
                yield Static(
                    Text.assemble(
                        "Приоритет →: ",
                        ("А — запись", "green"), ", ",
                        ("О — очередь", "yellow"), ", ",
                        ("М — места (свободные)", "bright_blue"), ", ",
                        ("П — прогноз", "magenta"), ".",
                    ),
                    id="calendar-legend", markup=False,
                )
                yield DataTable(id="calendar", cursor_type="cell")
            with TabPane("Очередь", id="queue-tab"):
                yield DataTable(id="queue", cursor_type="row")
            with TabPane("Прогнозы", id="forecasts-tab"):
                yield DataTable(id="forecasts", cursor_type="row")
            with TabPane("Записи", id="bookings-tab"):
                yield DataTable(id="bookings", cursor_type="row")
            with TabPane("Аккаунт", id="account-tab"):
                yield Static("", id="account", markup=False)
                with Horizontal(id="account-actions"):
                    yield Button("Вход", id="account-login")
                    yield Button("Забыть вход", id="account-logout")
                    yield Button("ntfy", id="account-ntfy")
                yield Button("Включить форсирование", id="account-force")
        yield Footer()

    def on_mount(self):
        self.query_one("#schedule", DataTable).add_columns("ID", "МСК", "Секция", "Зал", "Места", "Доступность", "Очередь")
        self.query_one("#calendar", DataTable).add_columns("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
        self.query_one("#queue", DataTable).add_columns("ID", "МСК", "Секция", "Статус")
        self.query_one("#forecasts", DataTable).add_columns("Прототип", "Начало (МСК)", "Статус / причина", "Реальный ID", "Проверено (МСК)", "Отличия")
        self.query_one("#bookings", DataTable).add_columns("ID", "МСК", "Секция", "Зал")
        self._render()
        self.update_task = asyncio.create_task(self._check_update())
        self.set_interval(0.5, self._tick)
        if self.store.data["session"] is None:
            self.action_login()
        else:
            self.action_refresh()

    def on_unmount(self):
        self.update_task.cancel()

    async def _check_update(self):
        try:
            notice = await asyncio.to_thread(check_update)
        except Exception:
            return
        if not notice or self.stopping:
            return
        self.update_notice = notice
        widget = self.query_one("#update-notice", Static)
        widget.update(notice)
        widget.display = True
        if isinstance(self.screen, LoginScreen):
            for widget in self.screen.query("#login-update"):
                widget.update(notice)
                widget.display = True

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
            f"ИТМО: {'вход выполнен' if session else 'нужен вход'}{expiry}\nАвтозапись: {'включена' if self.auto else 'на паузе'}{' (служба)' if self.service_active else ' (только TUI)'} | Последняя проверка: {checked}"
        )
        forced = self.store.data["force_limits"]
        self.query_one("#force-warning", Static).display = forced
        self.query_one("#opening-warning", Static).display = self.auto and self.opening_active
        self.query_one("#account-force", Button).label = "Отключить форсирование" if forced else "Включить форсирование"
        isu = clean_text(str(session.get("isu") or "")) if session else ""
        self.query_one("#account", Static).update(
            f"Состояние: {'вход выполнен' if session else 'нужен вход'}\n"
            f"Номер ИСУ: {isu or 'не указан'}\n"
            f"Срок сессии (МСК):{expiry or ' не указан'}\n"
            f"Автозапись: {'включена' if self.auto else 'на паузе'}\n"
            f"Уведомления ntfy: {'включены' if self.store.data['ntfy'] else 'отключены'}\n"
            f"Форсирование локальных лимитов: {'включено' if forced else 'выключено'}"
        )
        stale = (
            self.bookings_stale
            if self.query_one(TabbedContent).active == "bookings-tab"
            else self.stale or self.filter_stale
        )
        messages = [
            ("Данные устарели. " if stale else "") + self.error,
            self.service_notice,
            "Фоновая автозапись на паузе. p — возобновить после окончания блокировки." if self.store.data["background_paused"] else "",
        ]
        self.query_one("#error", Static).update(
            "\n".join(message for message in messages if message)
        )
        queue = self.agent.queue() if session else []
        self.query_one("#date-filter", Static).update(
            f"Дата (МСК): {self.selected_date.strftime('%d.%m.%Y')} — «Сброс» покажет все даты"
            if self.selected_date else "Дата: все (выбор во вкладке «Календарь»)"
        )
        self._render_calendar(queue)
        table = self.query_one("#schedule", DataTable)
        scroll = (table.scroll_x, table.scroll_y)
        selected = self.hidden_schedule_selection or self._selected(table) or self.schedule_selection
        table.clear()
        forecasts = forecast_prototypes(self.agent.snapshot, self.now())
        visible = [(lesson, False) for lesson in self.agent.snapshot if self._matches_filters(lesson)]
        visible += [(lesson, True) for lesson in forecasts if self._matches_filters(lesson, forecast=True)]
        visible_ids = {f"forecast:{lesson.id}" if forecast else str(lesson.id) for lesson, forecast in visible}
        self.hidden_schedule_selection = selected if selected and selected not in visible_ids else None
        for lesson, forecast in visible:
            queued = [
                x for x in queue
                if (x.get("forecast", {}).get("prototype_lesson_id") == lesson.id
                    if forecast else x["lesson_id"] == lesson.id and not (
                        x.get("forecast") and not x["forecast"]["bound"]))
            ]
            availability = "Неизвестно" if forecast else ("Да" if lesson.can_sign_in else "Нет")
            if not forecast and lesson.intersection:
                availability += "; Пересечение с расписанием"
            starts_at = lesson.starts_at + timedelta(days=14) if forecast else lesson.starts_at
            table.add_row(
                Text(f"~{lesson.id} (прототип)" if forecast else str(lesson.id)),
                Text(starts_at.astimezone(MSK).strftime("%d.%m %H:%M")),
                Text(("Прогноз: " if forecast else "") + clean_text(lesson.section_name)),
                Text(clean_text(lesson.room_name)),
                Text("Неизвестно" if forecast else str(lesson.available)),
                Text(availability),
                Text("; ".join(STATUSES[x["status"]] + (" (прогноз)" if x.get("forecast") is not None else "") for x in queued)),
                key=f"forecast:{lesson.id}" if forecast else str(lesson.id),
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
        table.call_after_refresh(table.scroll_to, *scroll, animate=False)
        self.schedule_selection = selected
        table = self.query_one("#queue", DataTable)
        scroll = (table.scroll_x, table.scroll_y)
        selected = self._selected(table)
        table.clear()
        for item in queue:
            status = STATUSES[item["status"]]
            if item.get("forecast") is not None:
                status += " — прогноз"
            if item["refusal_code"] is not None:
                status += f" ({item['refusal_code']})"
            table.add_row(
                Text(str(item["lesson_id"])),
                Text(
                    datetime.fromisoformat(item["starts_at"])
                    .astimezone(MSK)
                    .strftime("%d.%m %H:%M")
                ),
                Text(item["section_name"]),
                Text(status),
                key=self._queue_key(item),
            )
        if not queue:
            table.add_row(Text(""), Text("Очередь пуста"), Text(""), Text(""))
        self._restore(table, selected)
        table.call_after_refresh(table.scroll_to, *scroll, animate=False)
        table = self.query_one("#forecasts", DataTable)
        scroll = (table.scroll_x, table.scroll_y)
        selected = self._selected(table)
        table.clear()
        diagnostics = self.agent.forecast_diagnostics() if session else []
        for report in diagnostics:
            checked_at = report["checked_at"]
            table.add_row(
                Text(str(report["prototype_id"])),
                Text(datetime.fromisoformat(report["starts_at"]).astimezone(MSK).strftime("%d.%m.%Y %H:%M")),
                Text(FORECAST_STATUSES.get(report["status"], clean_text(report["status"]))),
                Text(str(report["real_id"]) if report["real_id"] is not None else "—"),
                Text(datetime.fromtimestamp(checked_at, MSK).strftime("%d.%m.%Y %H:%M") if checked_at is not None else "—"),
                Text("; ".join(clean_text(item) for item in report["differences"]) if report["status"] == "matched" and report["differences"] else "—"),
                key=f"{report['prototype_id']}:{report['starts_at']}",
            )
        if not diagnostics:
            table.add_row(Text(""), Text("Нет прогнозов"), Text(""), Text(""), Text(""), Text(""))
        self._restore(table, selected)
        table.call_after_refresh(table.scroll_to, *scroll, animate=False)
        table = self.query_one("#bookings", DataTable)
        scroll = (table.scroll_x, table.scroll_y)
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
        table.call_after_refresh(table.scroll_to, *scroll, animate=False)

    def _matches_filters(self, lesson, *, forecast=False):
        search = self.query_one("#filter-search", Input).value.strip().casefold()
        sport_type = self.query_one("#filter-type", Select).value
        sign = self.query_one("#filter-sign", Select).value
        places = self.query_one("#filter-places", Select).value
        period = self.query_one("#filter-time", Select).value
        starts_at = lesson.starts_at + timedelta(days=14) if forecast else lesson.starts_at
        if self.selected_date is not None and starts_at.astimezone(MSK).date() != self.selected_date:
            return False
        hour = starts_at.astimezone(MSK).hour
        return (
            (not search or search in clean_text(lesson.section_name).casefold()
             or search in clean_text(lesson.room_name).casefold())
            and (sport_type == "all" or self.filter_ids is not None and lesson.id in self.filter_ids)
            and (sign == "all" or not forecast and (sign == "yes") == lesson.can_sign_in)
            and (places == "all" or not forecast and (places == "yes") == (lesson.available > 0))
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

    def _render_calendar(self, queue):
        marks = {}

        def mark(day, symbol):
            marks.setdefault(day, set()).add(symbol)

        for lesson in self.agent.snapshot:
            day = lesson.starts_at.astimezone(MSK).date()
            if lesson.available > 0 and lesson.can_sign_in and not lesson.signed:
                mark(day, "М")
            if lesson.signed:
                mark(day, "А")
        for lesson in forecast_prototypes(self.agent.snapshot, self.now()):
            mark((lesson.starts_at + timedelta(days=14)).astimezone(MSK).date(), "П")
        for item in queue:
            day = datetime.fromisoformat(item["starts_at"]).astimezone(MSK).date()
            mark(day, "О")
        for booking in self.bookings_snapshot:
            mark(booking.starts_at.astimezone(MSK).date(), "А")

        month = self.calendar_month
        self.query_one("#calendar-month", Static).update(month.strftime("%m.%Y") + " (МСК)")
        table = self.query_one("#calendar", DataTable)
        cursor = table.cursor_coordinate
        table.clear()
        self.calendar_days = calendar.monthcalendar(month.year, month.month)
        for week in self.calendar_days:
            cells = []
            for day in week:
                if not day:
                    cells.append(Text(""))
                    continue
                current = date(month.year, month.month, day)
                day_marks = marks.get(current, ())
                symbols = "".join(symbol for symbol in "МПОА" if symbol in day_marks)
                color = next((color for mark, color in (("А", "green"), ("О", "yellow"), ("М", "bright_blue"), ("П", "magenta")) if mark in day_marks), None)
                cells.append(Text(f"{day:02d} {symbols}", style=color))
            table.add_row(*cells)
        table.move_cursor(row=min(cursor.row, len(self.calendar_days) - 1), column=cursor.column, scroll=False)

    def _change_calendar_month(self, delta):
        month = self.calendar_month
        index = (month.year - 1) * 12 + month.month - 1 + delta
        if 0 <= index < 9999 * 12:
            self.calendar_month = date(index // 12 + 1, index % 12 + 1, 1)
            self._render()

    def on_data_table_cell_selected(self, event: DataTable.CellSelected):
        if event.data_table.id != "calendar":
            return
        row, column = event.coordinate
        day = self.calendar_days[row][column]
        if not day:
            return
        self.selected_date = self.calendar_month.replace(day=day)
        self.hidden_schedule_selection = None
        self.schedule_selection = None
        self._render()
        self.query_one(TabbedContent).active = "schedule-tab"
        self.query_one("#schedule", DataTable).focus()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "filter-clear":
            self.selected_date = None
            self.query_one("#filter-search", Input).value = ""
            for selector in ("#filter-type", "#filter-sign", "#filter-places", "#filter-time"):
                self.query_one(selector, Select).value = "all"
            self._render()
        elif event.button.id == "calendar-prev":
            self._change_calendar_month(-1)
        elif event.button.id == "calendar-next":
            self._change_calendar_month(1)
        elif event.button.id == "calendar-clear":
            self.selected_date = None
            self._render()
            self.query_one(TabbedContent).active = "schedule-tab"
            self.query_one("#schedule", DataTable).focus()
        elif event.button.id == "account-login":
            self.action_login()
        elif event.button.id == "account-logout":
            self.action_logout()
        elif event.button.id == "account-ntfy":
            self.push_screen(NtfyScreen(), lambda _: self._render())
        elif event.button.id == "account-force":
            if self.store.data["force_limits"]:
                self._set_force_limits(False)
            else:
                self.push_screen(ForceLimitsScreen(), self._confirm_force_limits)

    def _confirm_force_limits(self, confirmed):
        if confirmed:
            self._set_force_limits(True)

    def _set_force_limits(self, enabled):
        old = self.store.data["force_limits"]
        self.store.data["force_limits"] = enabled
        try:
            self.store.save()
        except StateError:
            self.store.data["force_limits"] = old
            self.error = "Не удалось сохранить настройку форсирования."
        else:
            self.auto_interval = auto_delay(self.now(), 10, forced=True) if enabled else auto_delay(self.now())
            if enabled:
                # This deadline is set only by LocalLimit; server blocks live in state.
                self.limit_until = 0
            if max(self.store.data["blocked_until"].values(), default=0) <= self.now():
                self.error = ""
        self._render()

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
                    self._stop_for_error(exc)
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
    def _queue_key(item):
        forecast = item.get("forecast")
        kind = "forecast" if forecast is not None and not forecast["bound"] else "lesson"
        return f"{item['lesson_id']}:{kind}"

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
                table.move_cursor(row=table.get_row_index(key), scroll=False)
            except Exception:
                pass

    def _stop_for_error(self, exc):
        self.auto = False
        self.agent.booking_enabled = False
        self.resume_after_dialog = False
        if isinstance(exc, (Restricted, NetworkBlocked)):
            self.store.data["background_paused"] = True
            try:
                self.store.save()
            except StateError as save_error:
                self.service_notice = (
                    f"Не удалось сохранить паузу: {save_error} "
                    "После выхода служба может продолжить работу; остановите её отдельно."
                )

    async def _probe_service(self):
        try:
            active = await asyncio.to_thread(self.service_probe)
        except Exception:
            active = False
        if self.stopping:
            return
        previous = self.service_active
        self.service_active = active
        if previous and not active:
            self.auto = False
            self.agent.booking_enabled = False
            self.service_notice = "Фоновая служба остановлена или недоступна. p включает автозапись только в TUI."
        elif active and not previous:
            self.service_notice = ""
            self.auto = bool(
                not self.suspended and self.store.data["session"]
                and not self.store.data["background_paused"]
            )
            self.agent.booking_enabled = self.auto
        if self.is_mounted and previous != active:
            self._render()

    def _start_service_probe(self):
        if self.service_probe is None:
            return None
        if self.service_probe_task is None or self.service_probe_task.done():
            self.service_probe_task = asyncio.create_task(self._probe_service())
        return self.service_probe_task

    def _start(self, auto=False, bookings_only=False):
        if self.busy and not self.busy.done() or self.stopping:
            return
        self.busy = asyncio.create_task(self._check(auto, bookings_only))
    async def _check(self, auto, bookings_only=False):
        schedule_updated = False
        if not auto:
            self.bookings_stale = True
        try:
            if auto and (not self.auto or self.stopping):
                return
            if auto and self.service_active and self.service_probe is not None:
                await self._start_service_probe()
                if not self.auto or self.stopping:
                    return
            self.sport.opening_window_active = bool(auto and opening_window(self.store, self.now()))
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
                self._stop_for_error(exc)
        finally:
            self.sport.opening_window_active = False
            if not auto and not self.stopping and self.store.data["session"]:
                try:
                    await self._refresh_sport_types()
                except (AuthError, SportError, StateError) as exc:
                    self.error = str(exc)
                    if isinstance(exc, LocalLimit):
                        self.limit_until = exc.until
                    if isinstance(exc, (Restricted, LoginRequired, NetworkBlocked)):
                        self._stop_for_error(exc)
            session = self.store.data["session"]
            if not auto and not self.stopping and session and not session.get("isu") and not self.isu_checked:
                self.isu_checked = True
                try:
                    await self.sport.account_isu()
                except (AuthError, SportError, StateError) as exc:
                    if isinstance(exc, LocalLimit):
                        self.limit_until = exc.until
                    if isinstance(exc, (Restricted, LoginRequired, NetworkBlocked)):
                        self._stop_for_error(exc)
                        self.error = str(exc)
            if auto:
                self.auto_interval = (
                    10.0 if self.opening_active else
                    auto_delay(self.now(), 10, forced=True)
                    if self.store.data["force_limits"] else auto_delay(self.now())
                )
                self.last_auto = self.monotonic()
                if self.store.data["session"]:
                    owner = self.store.data["session"]["sub"]
                    for item in self.agent.queue():
                        key = (owner, self._queue_key(item))
                        if item["status"] == "uncertain":
                            self.seen_uncertain.add(key)
                        elif item["status"] == "waiting":
                            self.seen_uncertain.discard(key)
                    if not self.service_active and not any(
                        x["status"] == "waiting" for x in self.agent.queue()
                    ):
                        self.auto = False
                        self.agent.booking_enabled = False
            if schedule_updated and self.filter_type != "all" and not self.stopping:
                self.filter_generation += 1
                self.filter_stale = True
                self._start_filter()
            self._render()

    def _tick(self):
        if self.stopping:
            return
        if self.service_probe is not None and self.monotonic() >= self.next_service_probe:
            self.next_service_probe = self.monotonic() + 1.0
            self._start_service_probe()
        if (
            not self.auto
            or self.busy
            and not self.busy.done()
            or not self.store.data["session"]
        ):
            return
        try:
            active = opening_window(self.store, self.now())
        except StateError as exc:
            self.opening_active = False
            self.error = str(exc)
            self._render()
            return
        if active != self.opening_active:
            self.opening_active = active
            self._render()
        if not active and self.now() < self.limit_until:
            return
        queue = self.agent.queue()
        owner = self.store.data["session"]["sub"]
        if not any(
            x["status"] == "waiting"
            or x["status"] == "uncertain" and (owner, self._queue_key(x)) not in self.seen_uncertain
            for x in queue
        ):
            return
        if not active and not self.store.data["force_limits"] and self.last_auto is None and any(x["status"] == "waiting" for x in queue) and opening_wait(self.now()):
            return
        interval = 10.0 if active else self.auto_interval
        if not active and not self.store.data["force_limits"]:
            interval = max(120.0, interval)
        if self.last_auto is None or self.monotonic() - self.last_auto >= interval:
            self._start(True)

    def action_refresh(self):
        self._start(
            False, bookings_only=self.query_one(TabbedContent).active == "bookings-tab"
        )

    def action_pause(self):
        enable = not self.auto
        if enable:
            until = max(self.store.data["blocked_until"].values(), default=0)
            if until > self.now():
                self.error = str(NetworkBlocked(until))
                self._render()
                return
        self.auto = False
        self.agent.booking_enabled = False
        if self.service_active:
            old = self.store.data["background_paused"]
            self.store.data["background_paused"] = not enable
            try:
                self.store.save()
            except StateError as exc:
                self.store.data["background_paused"] = old
                self.error = (
                    f"Не удалось сохранить паузу: {exc} "
                    "После выхода служба может продолжить работу; остановите её отдельно."
                )
                self._render()
                return
        self.auto = enable
        self.agent.booking_enabled = enable
        self.error = ""
        self._render()
        self._tick()
    def action_login(self):
        self.suspended = True
        self.resume_after_dialog = self.auto and not self.service_active
        self.auto = False
        self.agent.booking_enabled = False
        self.agent.cancel_enabled = False
        async def open_login():
            if self.busy and not self.busy.done():
                await self.busy
            self.push_screen(LoginScreen(), self._login_result)
        asyncio.create_task(open_login())
    def _login_result(self, logged_in):
        self.suspended = False
        self.auto = bool(self.store.data["session"] and (
            (self.resume_after_dialog and not self.service_active)
            or (self.service_active and not self.store.data["background_paused"])
        ))
        self.agent.booking_enabled = self.auto
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
        self.suspended = True
        self.resume_after_dialog = self.auto and not self.service_active
        self.auto = False
        self.agent.booking_enabled = False
        self.agent.cancel_enabled = False
        self.push_screen(ConfirmScreen("Забыть локальный вход? Серверная сессия останется активной."), self._logout_result)
    def _logout_result(self, confirmed):
        if confirmed:
            asyncio.create_task(self._logout_after_work())
        else:
            self.suspended = False
            self.auto = bool(self.store.data["session"] and (
                (self.resume_after_dialog and not self.service_active)
                or (self.service_active and not self.store.data["background_paused"])
            ))
            self.agent.booking_enabled = self.auto
            self._render()
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
        self.suspended = False
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
                is_forecast = key.startswith("forecast:")
                candidates = forecast_prototypes(self.agent.snapshot, self.now()) if is_forecast else self.agent.snapshot
                lesson = next((x for x in candidates if (f"forecast:{x.id}" if is_forecast else str(x.id)) == key and self._matches_filters(x, forecast=is_forecast)), None)
                if lesson is None:
                    raise QueueError("Занятие не найдено в актуальном расписании.")
                if self.stale or self.filter_stale:
                    raise QueueError("Расписание устарело. Обновите его.")
                if is_forecast:
                    self.agent.add_forecast(lesson)
                    self.error = "Прогноз занятия добавлен в очередь."
                else:
                    self.agent.add(lesson)
                    self.error = ""
            except (QueueError, LoginRequired, StateError) as exc:
                self.error = str(exc)
            self._render()
            self._tick()
        elif event.data_table.id == "bookings":
            self._confirm_cancel(key)
        elif event.data_table.id == "queue":
            item = next(
                (x for x in self.agent.queue() if self._queue_key(x) == key), None
            )
            if item and item["status"] == "uncertain":
                self.push_screen(
                    ConfirmScreen(
                        "Проверьте запись в My ITMO. Если записи нет, разрешить новую попытку?"
                    ),
                    lambda yes: self._retry(key, yes),
                )

    def _retry(self, key, yes):
        if yes:
            try:
                ident, kind = key.split(":")
                self.agent.retry(int(ident), verified=True, forecast=kind == "forecast")
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
        item = next((x for x in self.agent.queue() if self._queue_key(x) == key), None)
        if item and item["status"] == "uncertain":
            self.push_screen(
                ConfirmScreen(
                    "Проверьте запись в My ITMO. Удалить локальную запись с неизвестным исходом?"
                ),
                lambda yes: self._remove(key, yes),
            )
        else:
            self._remove(key, True)

    def _remove(self, key, yes):
        if yes:
            try:
                ident, kind = key.split(":")
                self.agent.remove(int(ident), verified=True, forecast=kind == "forecast")
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
                self._stop_for_error(exc)
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
            from .cli import _service_notice, _service_running

            notice = _service_notice(
                store.directory.expanduser().absolute(), install=True
            )
            directory = store.directory.expanduser().absolute()
            active = _service_running(directory)
            if not active and not notice:
                notice = "Фоновая служба остановлена или недоступна. p включает автозапись только в TUI."
            KronbotApp(
                store, service_notice=notice, service_active=active,
                service_probe=lambda: _service_running(directory),
            ).run()
        return 0
    except StateLocked as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except StateError as exc:
        print(str(exc), file=sys.stderr)
        return 1
