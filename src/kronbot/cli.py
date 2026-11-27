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
import warnings
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .agent import Agent, MSK, QueueError
from .agent import MSK, Agent, QueueError, auto_delay, opening_wait
from .auth import AuthError, ItmoAuth, LoginRequired, Restricted
from .browser import BrowserError
from .sport import LocalLimit, NetworkBlocked, SportClient, SportError, clean_text
from .state import StateError, StateLocked, StateStore, state_directory

STATUSES = {"waiting": "Ожидает", "uncertain": "Проверьте в My ITMO", "booked": "Записан", "rejected": "Отказ", "expired": "Истекло"}
SERVICE_PAUSED_NOTICE = "Фоновая автозапись приостановлена. После окончания блокировки выполните kronbot service start."


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
    for action in ("add", "remove", "retry"):
        command = actions.add_parser(action)
        command.add_argument("id", type=positive_id, metavar="ID")
        if action in ("remove", "retry"):
            command.add_argument("--verified-not-booked", action="store_true", help="Подтверждаю самостоятельную проверку в My ITMO")
    book = sub.add_parser("book", help="Попробовать записаться один раз")
    book.add_argument("id", type=positive_id, metavar="ID")
    sub.add_parser("watch", help="Ожидать места в переднем плане")
    browser = sub.add_parser("browser", help="Установить Chromium для веб-входа")
    browser_commands = browser.add_subparsers(dest="browser_command", required=True)
    install_browser = browser_commands.add_parser("install", help="Установить Chromium")
    install_browser.add_argument("--with-deps", action="store_true", help="Также установить системные библиотеки Linux (могут потребоваться права администратора)")
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
    if method == "refresh-token":
        print("При обмене старый refresh-токен инвалидируется; не делите его с другим клиентом.", file=sys.stderr)
    return "", _secret("Токен: ")


def _live_session(store, now):
    session = store.data["session"]
    if not session:
        raise LoginRequired("Нужен новый вход.")
    expiry = session["refresh_expires_at"] if session["refresh_token"] else session["access_expires_at"]
    if expiry is None or expiry <= now:
        raise LoginRequired("Нужен новый вход.")
    return session


def _item_line(item):
    starts = datetime.fromisoformat(item["starts_at"]).astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    status = STATUSES[item["status"]]
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
    if command == "queue" and args.queue_command in ("remove", "retry"):
        if args.queue_command == "remove":
            agent.remove(args.id, verified=args.verified_not_booked)
        else:
            agent.retry(args.id, verified=args.verified_not_booked)
        print("Локальная очередь обновлена.")
        return 0
    if command == "schedule" or command == "queue" and args.queue_command == "add":
        await agent.check(allow_booking=False)
        if command == "schedule":
            if not agent.snapshot:
                print("Нет занятий сегодня и на 14 дней вперёд")
            for lesson in agent.snapshot:
                date_text = lesson.starts_at.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
                warning = "  Пересечение с расписанием" if lesson.intersection else ""
                allowed = "да" if lesson.can_sign_in else "нет"
                print(f"{lesson.id}  {date_text}  {lesson.section_name}  {lesson.room_name}  {lesson.available}  Запись: {allowed}{warning}")
        else:
            lesson = next((x for x in agent.snapshot if x.id == args.id), None)
            if lesson is None:
                raise QueueError("Занятие не найдено в расписании.")
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
            item = next((x for x in agent.queue() if x["lesson_id"] == args.id), None)
            if item is None or item["status"] != "uncertain":
                raise
            print(_item_line(item))
            return 4
        item = next((x for x in agent.queue() if x["lesson_id"] == args.id), None)
        fresh_signed = any(x.id == args.id and x.signed for x in agent.snapshot)
        if args.id in agent.confirmed_ids or fresh_signed:
            print("Уже записан" if fresh_signed else "Записан")
            return 0
        if item is not None:
            print(_item_line(item))
        return 4
    if command == "watch":
        print("Автозапись запущена. Остановка: Ctrl+C.")
        previous = {x["lesson_id"]: x["status"] for x in agent.queue()}
        if any(status in ("waiting", "uncertain") for status in previous.values()):
            wait_until_open = opening_wait(now())
            if wait_until_open:
                try:
                    await asyncio.wait_for(cancel.wait(), timeout=wait_until_open)
                except TimeoutError:
                    pass
        while not cancel.is_set():
            queue = agent.queue()
            if not any(x["status"] in ("waiting", "uncertain") for x in queue):
                return 0
            delay = 120.0
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
            for ident in sorted(agent.confirmed_ids):
                print(f"{ident}  Записан")
                previous.pop(ident, None)
            for item in queue:
                if previous.get(item["lesson_id"]) != item["status"]:
                    print(_item_line(item))
                previous[item["lesson_id"]] = item["status"]
            if cancel.is_set():
                return 0
            if not any(x["status"] == "waiting" for x in queue):
                if any(x["status"] == "uncertain" for x in queue):
                    print("Проверьте запись в My ITMO; повторная попытка требует подтверждения.", file=sys.stderr)
                    return 4
                return 0
            delay = auto_delay(now(), delay)
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
                        print("Автозапись приостановлена из-за ограничения доступа; после окончания блокировки выполните kronbot service start.")
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

def main() -> int:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if not stream.isatty() and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
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
