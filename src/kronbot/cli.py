"""Команды без полноэкранного интерфейса."""

import argparse
import asyncio
import getpass
import os
import shutil
import signal
import subprocess
import sys
import time
import threading
import warnings
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from .agent import MSK, Agent, QueueError, opening_window
from .auth import AuthError, ItmoAuth, LoginRequired, Restricted
from .browser import BrowserError
from .login_guides import LOGIN_GUIDE
from .notifications import validate_url
from .sport import LocalLimit, NetworkBlocked, SportClient, SportError, clean_text, forecast_prototypes
from .state import StateError, StateLocked, StateStore, state_directory
from .updates import check_update

STATUSES = {"waiting": "Ожидает", "uncertain": "Проверьте в My ITMO", "booked": "Записан", "rejected": "Отказ", "expired": "Истекло"}
FORECAST_STATUSES = {
    "not_checked": "Ещё не проверен",
    "not_open": "Запись ещё не открыта",
    "no_match": "Подходящее занятие не найдено",
    "ambiguous": "Найдено несколько подходящих занятий; сопоставление неоднозначно",
    "incomplete": "Недостаточно данных для сопоставления",
    "matched": "Занятие сопоставлено",
}
SERVICE_PAUSED_NOTICE = "Фоновая автозапись на паузе. После окончания ограничений возобновите её клавишей p в TUI или командой kronbot service start."
FORCE_WARNING = (
    "ВНИМАНИЕ: форсированный режим отключает локальный лимит 60 запросов в час "
    "и сокращает интервал фоновой проверки до 10 секунд. Это повышает нагрузку "
    "и риск ограничений аккаунта. Серверные блокировки 403/429, авторизация, "
    "проверка попыток и защита неопределённого исхода сохраняются. "
    "Режим действует также на фоновую службу."
)


def _force_warning():
    text = FORCE_WARNING
    if sys.stderr.isatty():
        text = f"\033[31m{text}\033[0m"
    print(text, file=sys.stderr)


def _limits_command(args, store):
    if args.limits_command == "status":
        print("Форсированный режим включён." if store.data["force_limits"] else
              "Обычные локальные ограничения включены.")
        return 0
    forced = args.limits_command == "force"
    if forced:
        _force_warning()
        if not args.confirm_risk:
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                raise ValueError("Для подтверждения нужен терминал или флаг --confirm-risk.")
            print("Включить форсированный режим? [y/N]: ", end="", file=sys.stderr, flush=True)
            try:
                answer = input().strip().lower()
            except (EOFError, OSError):
                raise ValueError("Подтверждение не получено.") from None
            if answer not in ("y", "yes", "д", "да"):
                print("Форсированный режим не включён.")
                return 0
    old = store.data["force_limits"]
    store.data["force_limits"] = forced
    try:
        store.save()
    except StateError:
        store.data["force_limits"] = old
        raise
    print("Форсированный режим включён." if forced else "Обычные локальные ограничения включены.")
    return 0


def _warn_forced_state(directory=None):
    try:
        with StateStore(directory) as store:
            if store.data["force_limits"]:
                _force_warning()
    except (StateLocked, StateError):
        pass



def positive_id(value):
    try:
        number = int(value)
        if number <= 0:
            raise ValueError()
        return number
    except ValueError:
        raise argparse.ArgumentTypeError("ID должен быть положительным целым.") from None


def parser():
    result = argparse.ArgumentParser(prog="kronbot", description="Автозапись на физкультуру ИТМО.")
    sub = result.add_subparsers(dest="command")
    sub.add_parser("tui", help="Открыть терминальный интерфейс")
    login = sub.add_parser("login", help="Войти в ITMO.ID")
    login.add_argument("method", nargs="?", choices=("browser", "password", "access-token", "refresh-token"),
                       help="Без аргумента откроется пошаговый выбор")
    sub.add_parser("logout", help="Локально забыть вход")
    sub.add_parser("schedule", help="Расписание сегодня и на 14 дней вперёд")
    sub.add_parser("bookings", help="Показать активные записи My ITMO")
    cancel = sub.add_parser("cancel", help="Отписаться от занятия в My ITMO")
    cancel.add_argument("id", type=positive_id, metavar="ID")
    cancel.add_argument(
        "--yes",
        action="store_true",
        help="Подтвердить отмену без интерактивного вопроса",
    )
    cancel.add_argument(
        "--whole-group",
        action="store_true",
        help="Разрешить отмену всей семестровой группы",
    )
    queue = sub.add_parser("queue", help="Управлять локальной очередью")
    actions = queue.add_subparsers(dest="queue_command", required=True)
    actions.add_parser("list", help="Показать очередь")
    diagnose = actions.add_parser("diagnose", help="Показать локальную диагностику прогнозов")
    diagnose.add_argument("id", nargs="?", type=positive_id, metavar="ID",
                          help="ID прототипа; без ID показываются все прогнозы")
    for action in ("add", "forecast", "remove", "retry"):
        command = actions.add_parser(
            action, help="Добавить прогноз на 14 суток вперёд" if action == "forecast" else None
        )
        command.add_argument("id", type=positive_id, metavar="ID")
        if action in ("remove", "retry"):
            kind = command.add_mutually_exclusive_group()
            kind.add_argument("--forecast", dest="forecast", action="store_const", const=True,
                              default=None, help="Выбрать ещё не сопоставленный прогноз")
            kind.add_argument("--lesson", dest="forecast", action="store_const", const=False,
                              help="Выбрать занятие или сопоставленный прогноз")
            command.add_argument("--verified-not-booked", action="store_true", help="Подтверждаю самостоятельную проверку в My ITMO")
    book = sub.add_parser("book", help="Попробовать записаться один раз")
    book.add_argument("id", type=positive_id, metavar="ID")
    sub.add_parser("watch", help="Ожидать места в переднем плане")
    limits = sub.add_parser("limits", help="Управлять локальными ограничениями")
    limits_commands = limits.add_subparsers(dest="limits_command", required=True)
    force = limits_commands.add_parser("force", help="Отключить локальные лимиты после подтверждения риска")
    force.add_argument("--confirm-risk", action="store_true", help="Явно подтвердить риск без интерактивного вопроса")
    limits_commands.add_parser("normal", help="Восстановить обычные локальные ограничения")
    limits_commands.add_parser("status", help="Показать режим локальных ограничений")
    sub.add_parser("doctor", help="Проверить и восстановить фоновую службу")
    browser = sub.add_parser("browser", help="Установить Chromium для веб-входа")
    browser_commands = browser.add_subparsers(dest="browser_command", required=True)
    install_browser = browser_commands.add_parser("install", help="Установить Chromium")
    install_browser.add_argument("--with-deps", action="store_true", help="Также установить системные библиотеки Linux (могут потребоваться права администратора)")
    ntfy = sub.add_parser("ntfy", help="Управлять уведомлениями ntfy")
    ntfy_commands = ntfy.add_subparsers(dest="ntfy_command", required=True)
    ntfy_commands.add_parser("set", help="Настроить уведомления")
    ntfy_commands.add_parser("status", help="Показать состояние уведомлений")
    ntfy_commands.add_parser("disable", help="Отключить уведомления")
    service = sub.add_parser("service", help="Управлять фоновой службой")
    service_commands = service.add_subparsers(dest="service_command", required=True)
    for action, description in (("install", "Установить и запустить"), ("start", "Запустить"),
                                ("stop", "Остановить"), ("status", "Показать состояние"),
                                ("uninstall", "Удалить")):
        service_commands.add_parser(action, help=description)
    run = service_commands.add_parser("run", help="Запуск цикла службы (используется системой)")
    run.add_argument("--state-dir", type=Path, required=True)
    return result


def _secret(prompt):
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ValueError("Для безопасного ввода нужен терминал.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except (getpass.GetPassWarning, EOFError, OSError):
        raise ValueError("Не удалось отключить эхо при вводе секрета.") from None

def _ntfy_command(args, store):
    if args.ntfy_command == "status":
        settings = store.data["ntfy"]
        if settings is None:
            print("Уведомления ntfy отключены.")
        else:
            print("Уведомления ntfy включены.")
            if settings["last_error"] is not None:
                print("Последняя ошибка приложения отмечена для уведомления.")
        return 0
    if args.ntfy_command == "disable":
        settings = None
    else:
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            raise ValueError("Для безопасного ввода нужен терминал.")
        try:
            url = input("HTTPS URL канала ntfy: ")
        except (EOFError, OSError):
            raise ValueError("Не удалось прочитать адрес канала ntfy.") from None
        try:
            url = validate_url(url)
        except ValueError:
            raise ValueError("Некорректный HTTPS URL канала ntfy.") from None
        token = _secret("Bearer-токен ntfy (необязательно): ") or None
        settings = {"url": url, "token": token, "last_error": None}
    old = store.data["ntfy"]
    store.data["ntfy"] = settings
    try:
        store.save()
    except StateError:
        store.data["ntfy"] = old
        raise
    print("Уведомления ntfy отключены." if settings is None else "Уведомления ntfy настроены.")
    return 0


def _choose_login():
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ValueError("Для выбора способа входа нужен терминал.")
    print("Вход в ИТМО — выберите способ:\n"
          "1. Веб-вход (рекомендуется): отдельно откроется ITMO.ID; сначала выполните kronbot browser install.\n"
          "2. Логин и пароль: введите данные в терминале; при MFA используйте веб-вход.\n"
          "3. Refresh-токен My ITMO: вставьте свой токен; при обмене старый будет инвалидирован.\n"
          "4. Access-токен My ITMO: вставьте свой токен; автоматически он не продлевается.")
    try:
        choice = input("Номер способа (1–4): ").strip()
        return {"1": "browser", "2": "password", "3": "refresh-token", "4": "access-token"}[choice]
    except (KeyError, EOFError, OSError):
        raise ValueError("Способ входа не выбран.") from None



def _text_login(method):
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ValueError("Для безопасного ввода нужен терминал.")
    if method == "password":
        try:
            username = input("Логин: ")
        except (EOFError, OSError):
            raise ValueError("Не удалось прочитать логин.") from None
        return username, _secret("Пароль: ")
    print(LOGIN_GUIDE["refresh" if method == "refresh-token" else "access"], file=sys.stderr)
    return "", _secret("Токен: ")


def _live_session(store, now):
    session = store.data["session"]
    if not session:
        raise LoginRequired("Нужен новый вход.")
    expiry = session["refresh_expires_at"] if session["refresh_token"] else session["access_expires_at"]
    if expiry is None or expiry <= now:
        raise LoginRequired("Нужен новый вход.")
    return session


def _unbound_forecast(item):
    forecast = item.get("forecast")
    return forecast is not None and not forecast["bound"]


def _item_line(item):
    starts = datetime.fromisoformat(item["starts_at"]).astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    status = STATUSES[item["status"]]
    if item.get("forecast") is not None:
        status += " — прогноз"
    status += " [--forecast]" if _unbound_forecast(item) else " [--lesson]"
    if item["refusal_code"] is not None:
        status += f" ({item['refusal_code']})"
    return f"{item['lesson_id']}  {starts}  {clean_text(item['section_name'])}  {status}"


async def execute(args, store, sport, *, cancel=None, now=time.time, monotonic=time.monotonic):
    """Выполнить команду с уже заблокированным хранилищем."""
    cancel = cancel or asyncio.Event()
    agent = Agent(store, sport, now=now, monotonic=monotonic)
    command = args.command
    if command == "logout":
        store.data["session"] = None
        store.save()
        print("Локальный вход забыт; серверная сессия не завершена.")
        return 0
    if command == "login":
        method = args.method or _choose_login()
        username, secret = ("", "") if method == "browser" else _text_login(method)
        sport.check_blocks()
        auth = ItmoAuth(on_restricted=sport._auth_restricted, now=now)
        if method == "browser":
            session = await auth.login_browser(cancel)
        elif method == "password":
            session = await auth.login_password(username, secret)
        elif method == "access-token":
            session = await auth.import_access(secret)
        else:
            session = await auth.import_refresh(secret)
        username = secret = ""
        if cancel.is_set():
            return 0
        old = store.data["session"]
        store.data["session"] = asdict(session)
        try:
            store.save()
        except StateError:
            store.data["session"] = old
            raise
        if session.isu is None and not cancel.is_set():
            try:
                await sport.account_isu()
            except (AuthError, SportError, StateError):
                print("Не удалось получить номер ИСУ; вход сохранён.", file=sys.stderr)
        print(f"Вход выполнен. ИСУ: {store.data['session'].get('isu') or 'не указан'}")
        return 0
    _live_session(store, now())
    if command == "bookings":
        bookings = await agent.bookings()
        if not bookings:
            print("Нет активных и предстоящих записей на ближайшие 365 дней.")
        for booking in bookings:
            when = booking.starts_at.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
            kind = (
                "разовое занятие"
                if booking.section_level == 1
                else "семестровая группа"
                if booking.section_level in (2, 3, 4)
                else "тип занятия неизвестен"
            )
            if booking.starts_at.timestamp() <= now():
                kind += ", занятие уже идёт"
            print(
                f"{booking.id}  {when}  {booking.section_name}  {booking.room_name}  {kind}"
            )
        return 0
    if command == "cancel":
        bookings = await agent.bookings()
        booking = next((item for item in bookings if item.id == args.id), None)
        if booking is None:
            raise QueueError("Активная запись не найдена.")
        if booking.starts_at.timestamp() <= now():
            raise QueueError(
                "Занятие уже началось. Проверьте возможность отписки в My ITMO."
            )
        group = booking.section_level in (2, 3, 4)
        if group and not args.whole_group:
            raise QueueError(
                "Это запись на семестровую группу. Для отмены всей группы укажите --whole-group."
            )
        if not args.yes:
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                raise ValueError("Для подтверждения нужен терминал или флаг --yes.")
            warning = (
                "Отписаться от всей семестровой группы"
                if group
                else "Отписаться от занятия"
            )
            try:
                answer = (
                    input(f"{warning} {booking.id} в My ITMO? [y/N]: ").strip().lower()
                )
            except (EOFError, OSError):
                raise ValueError("Подтверждение не получено.") from None
            if answer not in ("y", "yes", "д", "да"):
                print("Отмена записи не выполнена.")
                return 0
        await agent.cancel_booking(booking)
        print("Запись отменена в My ITMO.")
        return 0
    if command == "queue" and args.queue_command == "list":
        queue = agent.queue()
        if not queue:
            print("Очередь пуста")
        for item in queue:
            print(_item_line(item))
        return 0
    if command == "queue" and args.queue_command == "diagnose":
        reports = agent.forecast_diagnostics()
        if args.id is not None:
            reports = [report for report in reports if report["prototype_id"] == args.id]
            if not reports:
                raise QueueError("Прогноз с этим ID прототипа не найден.")
        if not reports:
            print("Нет прогнозов для диагностики.")
        for report in reports:
            starts = datetime.fromisoformat(report["starts_at"]).astimezone(MSK)
            checked = report["checked_at"]
            checked_text = (
                datetime.fromtimestamp(checked, MSK).strftime("%d.%m.%Y %H:%M:%S МСК")
                if checked is not None else "ещё не проверялся"
            )
            real_id = report["real_id"]
            print(f"Прототип: {report['prototype_id']}  Начало: {starts:%d.%m.%Y %H:%M} МСК")
            print(f"  Статус: {FORECAST_STATUSES[report['status']]}")
            print(f"  ID реального занятия: {real_id if real_id is not None else 'не определён'}")
            print(f"  Последняя проверка: {checked_text}")
            if report["status"] == "matched":
                for difference in report["differences"]:
                    print(f"  Отличие: {clean_text(difference)}")
        print("Свежие данные появляются после фоновой проверки или команды kronbot schedule.")
        return 0
    if command == "queue" and args.queue_command in ("remove", "retry"):
        if args.forecast is None and sum(item["lesson_id"] == args.id for item in agent.queue()) > 1:
            raise QueueError("Укажите --forecast для прогноза или --lesson для занятия.")
        if args.queue_command == "remove":
            agent.remove(args.id, verified=args.verified_not_booked, forecast=args.forecast)
        else:
            agent.retry(args.id, verified=args.verified_not_booked, forecast=args.forecast)
        print("Локальная очередь обновлена.")
        return 0
    if command == "schedule" or command == "queue" and args.queue_command in ("add", "forecast"):
        await agent.check(allow_booking=False)
        if command == "schedule":
            if not agent.snapshot:
                print("Нет занятий сегодня и на 14 дней вперёд")
            for lesson in agent.snapshot:
                date_text = lesson.starts_at.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
                warning = "  Пересечение с расписанием" if lesson.intersection else ""
                allowed = "да" if lesson.can_sign_in else "нет"
                print(f"{lesson.id}  {date_text}  {lesson.section_name}  {lesson.room_name}  {lesson.available}  Запись: {allowed}{warning}")
            for lesson in forecast_prototypes(agent.snapshot, now()):
                target = (lesson.starts_at + timedelta(days=14)).astimezone(MSK)
                date_text = target.strftime("%d.%m.%Y %H:%M")
                print(
                    f"~ {lesson.id}  {date_text}  {clean_text(lesson.section_name)}  "
                    f"{clean_text(lesson.room_name)}  прогноз (ID прототипа; queue forecast {lesson.id})"
                )
        else:
            lesson = next((x for x in agent.snapshot if x.id == args.id), None)
            if lesson is None:
                raise QueueError("Занятие не найдено в расписании.")
            if args.queue_command == "forecast":
                agent.add_forecast(lesson)
                print("Прогноз занятия добавлен в очередь.")
            else:
                agent.add(lesson)
                print("Добавлено в очередь.")
        return 0
    if command == "book":
        previous = next((x["status"] for x in agent.queue() if x["lesson_id"] == args.id), None)
        try:
            await agent.check(allow_booking=True, only_lesson_id=args.id)
        except LocalLimit:
            raise
        except SportError:
            item = next((x for x in agent.queue() if x["lesson_id"] == args.id and not _unbound_forecast(x)), None)
            if item is None or item["status"] != "uncertain":
                raise
            print(_item_line(item))
            return 4
        item = next((x for x in agent.queue() if x["lesson_id"] == args.id and not _unbound_forecast(x)), None)
        fresh_signed = any(x.id == args.id and x.signed for x in agent.snapshot)
        if args.id in agent.confirmed_ids or fresh_signed:
            print("Уже записан" if fresh_signed else "Записан")
            return 0
        if item is not None:
            print(_item_line(item))
        return 4
    if command == "watch":
        print("Автозапись запущена. Остановка: Ctrl+C.")
        previous = {(x["lesson_id"], _unbound_forecast(x)): x["status"] for x in agent.queue()}
        window_announced = False
        while not cancel.is_set():
            queue = agent.queue()
            if not any(x["status"] in ("waiting", "uncertain") for x in queue):
                return 0
            active = opening_window(store, now())
            sport.opening_window_active = active
            if active and not window_announced:
                print(
                    "Окно открытия: на 5 минут временно отключён локальный лимит "
                    "60 запросов в час; серверные ограничения продолжают действовать.",
                    file=sys.stderr,
                )
            window_announced = active
            forced = store.data["force_limits"]
            delay = 10.0 if forced or active else 120.0
            try:
                await agent.check(allow_booking=True)
            except NetworkBlocked:
                raise
            except LocalLimit as exc:
                print(str(exc), file=sys.stderr)
                delay = max(delay, exc.until - now())
            except (Restricted, LoginRequired):
                raise
            except (SportError, AuthError) as exc:
                if cancel.is_set():
                    return 0
                print(str(exc), file=sys.stderr)
            finally:
                sport.opening_window_active = False
            for ident in sorted(agent.confirmed_ids):
                print(f"{ident}  Записан")
                previous.pop((ident, False), None)
            for item in queue:
                key = (item["lesson_id"], _unbound_forecast(item))
                if previous.get(key) != item["status"]:
                    print(_item_line(item))
                previous[key] = item["status"]
            if cancel.is_set():
                return 0
            if not any(x["status"] == "waiting" for x in queue):
                if any(x["status"] == "uncertain" for x in queue):
                    print("Проверьте запись в My ITMO; повторная попытка требует подтверждения.", file=sys.stderr)
                    return 4
                return 0
            local_now = datetime.fromtimestamp(now(), MSK)
            midnight = datetime.combine(local_now.date() + timedelta(days=1), datetime.min.time(), MSK)
            until_midnight = midnight.timestamp() - now()
            delay = min(delay, max(10.0, until_midnight))
            try:
                await asyncio.wait_for(cancel.wait(), timeout=delay)
            except TimeoutError:
                pass
        return 0
    return 2


async def _run(args, store):
    sport = SportClient(store)
    cancel = asyncio.Event()
    loop = asyncio.get_running_loop()
    old_handlers = {}
    def stop():
        cancel.set()
        sport.stopped = True
    def handler(signum, frame):
        loop.call_soon_threadsafe(stop)
    signals = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        signals.append(signal.SIGTERM)
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    try:
        for sig in signals:
            old_handlers[sig] = signal.signal(sig, handler)
        return await execute(args, store, sport, cancel=cancel)
    finally:
        for sig, previous in old_handlers.items():
            signal.signal(sig, previous)
        await sport.close()



def _service_manager():
    if sys.platform == "win32":
        from . import _service_windows
        return _service_windows
    if sys.platform == "darwin" or sys.platform.startswith("linux"):
        from . import _service_posix
        return _service_posix
    raise RuntimeError("Пользовательская служба не поддерживается на этой платформе.")


def _service_executable() -> str:
    invoked = Path(sys.argv[0])
    if invoked.name.lower() not in ("kronbot", "kronbot.exe"):
        found = shutil.which("kronbot")
        if not found:
            raise RuntimeError("Установите kronbot через uv tool install . и повторите.")
        invoked = Path(found)
    executable = invoked.absolute()
    if not executable.is_file():
        raise RuntimeError("Не найден установленный исполняемый файл kronbot.")
    return os.fspath(executable)


def _same_directory(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(
        str(right.resolve())
    )


def _service_notice(directory: Path, *, install: bool = False) -> str:
    """Проверить службу текущего каталога; TUI может установить отсутствующую."""
    try:
        manager = _service_manager()
        status = manager.status()
        if status == "not installed":
            if install:
                manager.install(_service_executable(), directory)
                return ""
            return "Фоновая служба не установлена. Выполните kronbot service install."
        target = manager.configured_state_dir()
        if target is None or not _same_directory(target, directory):
            return "Фоновая служба настроена для другого каталога состояния. Проверьте KRONBOT_STATE_DIR."
        if status != "running":
            return "Фоновая служба остановлена. Выполните kronbot service start."
        return ""
    except (RuntimeError, OSError, ValueError):
        return "Не удалось проверить или установить фоновую службу. Проверьте kronbot service status."


def _service_running(directory: Path) -> bool:
    """Проверить запущенную службу этого каталога без изменения системы."""
    try:
        manager = _service_manager()
        if manager.status() != "running":
            return False
        target = manager.configured_state_dir()
        return target is not None and _same_directory(target, directory)
    except (RuntimeError, OSError, ValueError):
        return False


def _service_command(args) -> int:
    action = args.service_command
    if action == "run":
        from .service import run_service
        if not args.state_dir.is_absolute():
            raise ValueError("Каталог состояния службы должен быть абсолютным.")
        return asyncio.run(run_service(args.state_dir))
    manager = _service_manager()
    directory = state_directory().expanduser().absolute()
    if action == "status":
        status = manager.status()
        print({"running": "Служба запущена", "stopped": "Служба остановлена",
               "not installed": "Служба не установлена"}[status])
        if status != "not installed":
            target = manager.configured_state_dir()
            if target is None or not _same_directory(target, directory):
                print(
                    "Фоновая служба настроена для другого каталога состояния. Проверьте KRONBOT_STATE_DIR."
                )
                return 0
            try:
                with StateStore(directory) as store:
                    if store.data["background_paused"]:
                        print(
                            SERVICE_PAUSED_NOTICE
                        )
                    elif store.data["session"] is None:
                        print("Для автозаписи нужен вход.")
            except StateLocked:
                print("Состояние очереди сейчас занято.")
        return 0
    if action in ("install", "start"):
        if action == "start":
            target = manager.configured_state_dir()
            if target is not None and not _same_directory(target, directory):
                raise RuntimeError(
                    "Фоновая служба настроена для другого каталога состояния. Проверьте KRONBOT_STATE_DIR."
                )
        with StateStore(directory) as store:
            until = max(store.data["blocked_until"].values(), default=0)
            if until > time.time():
                raise NetworkBlocked(until)
            if action == "install":
                manager.install(_service_executable(), directory)
            else:
                manager.start()
            if store.data["background_paused"]:
                store.data["background_paused"] = False
                store.save()
        print("Фоновая служба запущена.")
    elif action == "stop":
        manager.stop()
        print("Фоновая служба остановлена.")
    else:
        manager.uninstall()
        print("Фоновая служба удалена; очередь и вход сохранены.")
    return 0

def _doctor() -> int:
    """Проверить службу без изменения её определения и локального состояния."""
    directory = state_directory().expanduser().absolute()
    try:
        manager = _service_manager()
        status = manager.status()
        if status == "not installed":
            print("Фоновая служба не установлена. Установить: kronbot service install.")
            return 1
        target = manager.configured_state_dir()
        if target is None or not _same_directory(target, directory):
            print("Служба настроена для другого каталога состояния. Проверьте KRONBOT_STATE_DIR.", file=sys.stderr)
            return 1
        if status not in ("running", "stopped"):
            raise RuntimeError("Не удалось определить состояние службы.")
        with StateStore(directory) as store:
            try:
                _live_session(store, time.time())
            except LoginRequired:
                print("Для автозаписи нужен вход: kronbot login.")
            else:
                print("Вход доступен.")
            blocked = max(store.data["blocked_until"].values(), default=0) > time.time()
            paused = store.data["background_paused"]
            if status == "running":
                print("Фоновая служба запущена.")
                if blocked:
                    print("Действует сетевое ограничение; дождитесь его окончания.")
                if paused:
                    print(SERVICE_PAUSED_NOTICE)
                return 0
            if blocked:
                print("Служба остановлена; действует сетевое ограничение. Дождитесь его окончания.", file=sys.stderr)
                return 3
            if paused:
                print(f"Служба остановлена. {SERVICE_PAUSED_NOTICE}", file=sys.stderr)
                return 3
            manager.start()
        # The service needs the state lock to start; never hold it while verifying.
        deadline = time.monotonic() + 2
        while True:
            status = manager.status()
            if status == "running":
                print("Фоновая служба запущена.")
                return 0
            if status != "stopped" or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        print("Служба не запустилась. Проверьте её состояние и путь к kronbot; при необходимости переустановите службу командами kronbot service uninstall и kronbot service install.", file=sys.stderr)
        return 1
    except StateLocked:
        print("Состояние занято; повторите kronbot doctor позже.", file=sys.stderr)
        return 1
    except (StateError, RuntimeError, OSError, ValueError):
        print("Не удалось проверить или запустить службу. Проверьте kronbot service status и путь к установленному kronbot; при необходимости выполните kronbot service uninstall, затем kronbot service install.", file=sys.stderr)
        return 1


def _interactive_update_notice() -> None:
    result = []

    def check():
        try:
            advice = check_update()
            if advice:
                result.append(advice)
        except Exception:
            pass

    worker = threading.Thread(target=check, daemon=True)
    worker.start()
    worker.join(1.5)
    if result:
        print(result[0], file=sys.stderr)


def main() -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if not stream.isatty() and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "ntfy":
        ntfy_args = sys.argv[2:]
        if not (ntfy_args in ([], ["-h"], ["--help"])
                or (ntfy_args[0] in ("set", "status", "disable")
                    and (len(ntfy_args) == 1 or ntfy_args[1:] in (["-h"], ["--help"])))):
            print("Используйте ntfy set, status или disable без дополнительных аргументов.", file=sys.stderr)
            return 2
    args = parser().parse_args()
    if args.command in (None, "tui"):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print("Для TUI нужен терминал. Список CLI-команд: kronbot --help.", file=sys.stderr)
            return 2
        try:
            from .tui import run
        except ImportError:
            print("Не найден TUI. Переустановите: uv tool install --force . или pipx reinstall kronbot", file=sys.stderr)
            return 2
        return run()
    if (args.command not in ("browser", "book", "watch", "service", "ntfy", "limits")
            and not (args.command == "queue" and args.queue_command == "diagnose")
            and sys.stdin.isatty() and sys.stderr.isatty()):
        _interactive_update_notice()
    if args.command == "limits":
        try:
            with StateStore() as store:
                if store.data["force_limits"] and args.limits_command != "force":
                    _force_warning()
                return _limits_command(args, store)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except (StateLocked, StateError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.command in ("doctor", "browser", "service"):
        directory = args.state_dir if args.command == "service" and args.service_command == "run" else None
        _warn_forced_state(directory)
    if args.command == "doctor":
        return _doctor()
    if args.command == "browser":
        command = [sys.executable, "-m", "playwright", "install"]
        if args.with_deps:
            command.append("--with-deps")
        command.append("chromium")
        try:
            return subprocess.run(command, shell=False, check=False).returncode
        except OSError:
            print("Не удалось запустить установку Chromium.", file=sys.stderr)
            return 1
    if args.command == "ntfy":
        try:
            with StateStore() as store:
                if store.data["force_limits"]:
                    _force_warning()
                return _ntfy_command(args, store)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except (StateLocked, StateError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.command == "service":
        try:
            return _service_command(args)
        except (ValueError, StateError, RuntimeError, LocalLimit) as exc:
            print(str(exc), file=sys.stderr)
            return 3 if isinstance(exc, LocalLimit) else 1
    notice = _service_notice(state_directory().expanduser().absolute())
    if notice:
        print(f"Предупреждение: {notice}", file=sys.stderr)
    try:
        with StateStore() as store:
            if store.data["force_limits"]:
                _force_warning()
            if store.data["background_paused"]:
                print(f"Предупреждение: {SERVICE_PAUSED_NOTICE}", file=sys.stderr)
            return asyncio.run(_run(args, store))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (LoginRequired, LocalLimit, Restricted) as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except QueueError as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except (StateLocked, StateError, AuthError, BrowserError, SportError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
