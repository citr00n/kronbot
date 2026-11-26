"""Команды без полноэкранного интерфейса."""

import argparse
import asyncio
import getpass
import signal
import sys
import time
import warnings
from dataclasses import asdict
from datetime import datetime

from .agent import Agent, MSK, QueueError
from .auth import AuthError, ItmoAuth, LoginRequired, Restricted
from .browser import BrowserError
from .notice import RISK_NOTICE
from .sport import LocalLimit, NetworkBlocked, SportClient, SportError, clean_text
from .state import StateError, StateLocked, StateStore

STATUSES = {"waiting": "Ожидает", "uncertain": "Проверьте в My ITMO", "booked": "Записан", "rejected": "Отказ", "expired": "Истекло"}


def positive_id(value):
    try:
        number = int(value)
        if number <= 0:
            raise ValueError()
        return number
    except ValueError:
        raise argparse.ArgumentTypeError("ID должен быть положительным целым.") from None


def parser():
    result = argparse.ArgumentParser(prog="kronbot", description="Неофициальный API My ITMO: проверьте правила использования.")
    result.add_argument("--accept-risk", action="store_true", help="Подтвердить риск использования неофициального API")
    sub = result.add_subparsers(dest="command")
    sub.add_parser("tui", help="Открыть терминальный интерфейс")
    login = sub.add_parser("login", help="Войти в ITMO.ID")
    login.add_argument("method", choices=("browser", "password", "access-token", "refresh-token"))
    sub.add_parser("logout", help="Локально забыть вход")
    sub.add_parser("schedule", help="Показать расписание на семь дней")
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
        username, secret = ("", "") if args.method == "browser" else _text_login(args.method)
        sport.check_blocks()
        auth = ItmoAuth(on_restricted=sport._auth_restricted, now=now)
        if args.method == "browser":
            session = await auth.login_browser(cancel)
        elif args.method == "password":
            session = await auth.login_password(username, secret)
        elif args.method == "access-token":
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
        print("Вход выполнен.")
        return 0
    _live_session(store, now())
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
                print("Нет занятий на ближайшие 7 дней")
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
        if fresh_signed:
            print("Уже записан")
            return 0
        if item is not None:
            print(_item_line(item))
            if item["status"] == "booked" and previous != "booked":
                return 0
        return 4
    if command == "watch":
        print("Автозапись запущена. Остановка: Ctrl+C.")
        previous = {x["lesson_id"]: x["status"] for x in agent.queue()}
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
            print("Установите TUI: uv run --locked --extra tui kronbot", file=sys.stderr)
            return 2
        return run()
    needs_risk = args.command in ("login", "schedule", "book", "watch") or args.command == "queue" and args.queue_command == "add"
    if needs_risk and not args.accept_risk:
        print(RISK_NOTICE, file=sys.stderr)
        return 2
    try:
        with StateStore() as store:
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
