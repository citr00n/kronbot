import os
import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

import httpx
import pytest

from kronbot.cli import execute, parser
from kronbot.sport import SportClient
from kronbot.state import StateStore


def invoke(*args, input=None, env=None):
    return subprocess.run([sys.executable, "-m", "kronbot", *args], input=input, text=True, capture_output=True, env=env)


def test_help_and_commands_without_tty(tmp_path):
    env = {**os.environ, "KRONBOT_STATE_DIR": str(tmp_path / "state")}
    help_result = invoke("--help", env=env)
    assert help_result.returncode == 0 and "schedule" in help_result.stdout
    assert not (tmp_path / "state").exists()
    booking = invoke("book", "500001", env=env)
    assert booking.returncode == 3 and "Нужен новый вход" in booking.stderr
    password = invoke("login", "password", input="user\nsecret\n", env=env)
    assert password.returncode == 2 and "терминал" in password.stderr
    assert "secret" not in password.stdout + password.stderr


async def test_queue_and_single_booking(tmp_path, capsys):
    now = 1900000000
    stamp = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append(request.content)
            return httpx.Response(200, json={"error_code": 0, "result": json.loads(request.content)})
        if request.url.path.endswith("/sign/schedule"):
            lessons = [{"id": ident, "date": stamp, "section_name": "Секция", "room_name": "Зал", "section_level": 1, "type_id": 2, "available": 1, "signed": False, "can_sign_in": {"can_sign_in": True}} for ident in (500001, 500002, 500003)]
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": lessons}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        return httpx.Response(200, json={"error_code": 0, "result": {"free_attempts": 1, "can_sign_in": True}})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        async def run(*argv):
            return await execute(parser().parse_args(list(argv)), store, client, now=lambda: now)
        assert await run("schedule") == 0
        assert "500001" in capsys.readouterr().out
        assert await run("queue", "add", "500002") == 0
        assert not posts
        assert await run("book", "500001") == 0
        assert posts == [b"[500001]"]
        assert await run("book", "500002") == 0
        assert posts == [b"[500001]", b"[500002]"]
        assert await run("queue", "add", "500003") == 0
        capsys.readouterr()
        assert await run("queue", "list") == 0
        output = capsys.readouterr().out
        assert "500003" in output and "500001" not in output and "500002" not in output
        assert await run("queue", "remove", "500003") == 0
        assert len(posts) == 2
        await client.close()


@pytest.mark.parametrize("stop_at_post", [False, True])
async def test_watch_stop_never_cancels_started_post(tmp_path, stop_at_post):
    now = 1900000000
    stamp = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
    reached, release, cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()
    posts = []
    async def handler(request):
        if request.method == "POST":
            posts.append(request.content)
            reached.set()
            await release.wait()
            return httpx.Response(200, json={"error_code": 0, "result": [1]})
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [{"id": 1, "date": stamp, "section_level": 1, "type_id": 2, "available": 1, "signed": False, "can_sign_in": {"can_sign_in": True}}]}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        if not stop_at_post:
            reached.set()
            await release.wait()
        return httpx.Response(200, json={"error_code": 0, "result": {"free_attempts": 1, "can_sign_in": True}})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        store.data["queues"]["owner"] = [{"lesson_id": 1, "section_name": "Секция", "starts_at": stamp, "status": "waiting", "refusal_code": None}]
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        task = asyncio.create_task(execute(parser().parse_args(["watch"]), store, client, cancel=cancel, now=lambda: now))
        await reached.wait()
        cancel.set()
        client.stopped = True
        release.set()
        assert await task == 0
        queue = store.data["queues"]["owner"]
        assert queue == [] if stop_at_post else queue[0]["status"] == "waiting"
        assert bool(posts) is stop_at_post
        await client.close()


async def test_book_reports_uncertain_post(tmp_path, capsys):
    now = 1900000000
    stamp = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
    def handler(request):
        if request.method == "POST":
            raise httpx.ReadError("private response")
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [{"id": 1, "date": stamp, "section_level": 1, "type_id": 2, "available": 1, "signed": False, "can_sign_in": {"can_sign_in": True}}]}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        return httpx.Response(200, json={"error_code": 0, "result": {"free_attempts": 1, "can_sign_in": True}})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        result = await execute(parser().parse_args(["book", "1"]), store, client, now=lambda: now)
        assert result == 4
        output = capsys.readouterr().out
        assert "Проверьте в My ITMO" in output and "private response" not in output
        assert store.data["queues"]["owner"][0]["status"] == "uncertain"
        await client.close()


def test_guided_login_selection(monkeypatch, capsys):
    from kronbot import cli
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "3")
    assert cli._choose_login() == "refresh-token"
    output = capsys.readouterr().out
    assert "инвалидирован" in output and "не продлевается" in output


def test_browser_install_uses_current_environment(monkeypatch):
    from types import SimpleNamespace
    from kronbot import cli
    calls = []
    monkeypatch.setattr(sys, "argv", ["kronbot", "browser", "install"])
    monkeypatch.setattr(cli.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0))
    assert cli.main() == 0
    assert calls == [([sys.executable, "-m", "playwright", "install", "chromium"], {"shell": False, "check": False})]


def test_service_install_and_resume(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from kronbot import cli
    directory = tmp_path / "state with spaces"
    calls = []
    manager = SimpleNamespace(install=lambda exe, path: calls.append(("install", exe, path)),
                              start=lambda: calls.append(("start",)), configured_state_dir=lambda: directory.absolute())
    monkeypatch.setattr(cli, "_service_manager", lambda: manager)
    monkeypatch.setattr(cli, "_service_executable", lambda: "/fake/kronbot")
    monkeypatch.setattr(cli, "state_directory", lambda: directory)
    assert cli._service_command(parser().parse_args(["service", "install"])) == 0
    assert calls == [("install", "/fake/kronbot", directory.absolute())]
    with StateStore(directory) as store:
        store.data["background_paused"] = True
        store.save()
    assert cli._service_command(parser().parse_args(["service", "start"])) == 0
    with StateStore(directory) as store:
        assert not store.data["background_paused"]
    assert calls[-1] == ("start",)


def test_failed_service_start_keeps_restriction_pause(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from kronbot import cli
    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["background_paused"] = True
        store.save()
    def fail():
        raise RuntimeError("Не удалось запустить службу.")
    monkeypatch.setattr(cli, "_service_manager", lambda: SimpleNamespace(start=fail, configured_state_dir=lambda: directory.absolute()))
    monkeypatch.setattr(cli, "state_directory", lambda: directory)
    with pytest.raises(RuntimeError):
        cli._service_command(parser().parse_args(["service", "start"]))
    with StateStore(directory) as store:
        assert store.data["background_paused"] is True


@pytest.mark.parametrize(("status", "target", "expected"), [
    ("not installed", None, "не установлена"),
    ("stopped", "current", "остановлена"),
    ("running", "other", "другого каталога"),
    ("running", "current", ""),
])
def test_service_notice(monkeypatch, tmp_path, status, target, expected):
    from types import SimpleNamespace
    from kronbot import cli
    current = tmp_path / "state"
    path = current if target == "current" else tmp_path / "other" if target == "other" else None
    manager = SimpleNamespace(status=lambda: status, configured_state_dir=lambda: path)
    monkeypatch.setattr(cli, "_service_manager", lambda: manager)
    notice = cli._service_notice(current)
    assert expected in notice if expected else notice == ""


def test_tui_auto_installs_missing_service_once(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from kronbot import cli
    current = tmp_path / "state"
    calls = []
    manager = SimpleNamespace(status=lambda: "not installed", install=lambda exe, path: calls.append((exe, path)))
    monkeypatch.setattr(cli, "_service_manager", lambda: manager)
    monkeypatch.setattr(cli, "_service_executable", lambda: "/fake/kronbot")
    assert cli._service_notice(current, install=True) == ""
    assert calls == [("/fake/kronbot", current)]


def test_service_start_never_resumes_another_state(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from kronbot import cli
    current = tmp_path / "current"
    calls = []
    manager = SimpleNamespace(configured_state_dir=lambda: tmp_path / "other", start=lambda: calls.append(True))
    monkeypatch.setattr(cli, "_service_manager", lambda: manager)
    monkeypatch.setattr(cli, "state_directory", lambda: current)
    with pytest.raises(RuntimeError, match="другого каталога"):
        cli._service_command(parser().parse_args(["service", "start"]))
    assert not calls


def test_cli_warns_when_background_service_is_missing(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace
    from kronbot import cli
    monkeypatch.setenv("KRONBOT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(sys, "argv", ["kronbot", "queue", "list"])
    monkeypatch.setattr(cli, "_service_manager", lambda: SimpleNamespace(status=lambda: "not installed"))
    assert cli.main() == 3
    assert "Фоновая служба не установлена" in capsys.readouterr().err


async def test_queue_forecast_and_disambiguated_removal(tmp_path, capsys):
    from datetime import timedelta
    from kronbot.agent import QueueError

    now = 1900000000
    start = datetime.fromtimestamp(now + 86400, timezone.utc)
    lesson = {
        "id": 10, "date": start.isoformat(),
        "date_end": (start + timedelta(hours=1)).isoformat(),
        "section_name": "Бег", "room_name": "Зал", "section_level": 1,
        "type_id": 2, "available": 1, "signed": False,
        "can_sign_in": {"can_sign_in": True}, "section_id": 1,
        "lesson_level": 1, "building_id": 2, "room_id": 3,
        "time_slot_id": 4, "teacher_isu": 5,
    }
    def handler(request):
        assert request.method == "GET"
        if request.url.path.endswith("/sign/schedule"):
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [lesson]}]})
        return httpx.Response(200, json={"error_code": 0, "result": []})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        async def run(*argv):
            return await execute(parser().parse_args(list(argv)), store, client, now=lambda: now)
        assert await run("queue", "add", "10") == 0
        assert await run("queue", "forecast", "10") == 0
        queue = store.data["queues"]["owner"]
        assert len(queue) == 2
        assert datetime.fromisoformat(queue[1]["starts_at"]) == start + timedelta(days=14)
        capsys.readouterr()
        assert await run("queue", "list") == 0
        output = capsys.readouterr().out
        assert "прогноз" in output and "[--forecast]" in output and "[--lesson]" in output
        with pytest.raises(QueueError):
            await run("queue", "remove", "10")
        assert await run("queue", "remove", "10", "--lesson") == 0
        assert len(queue) == 1 and queue[0]["forecast"]["bound"] is False
        assert await run("queue", "remove", "10", "--forecast") == 0
        assert not queue
        lesson.pop("teacher_isu")
        with pytest.raises(QueueError, match="нельзя создать"):
            await run("queue", "forecast", "10")
        await client.close()


@pytest.mark.parametrize(("status", "target", "expected"), [
    ("running", "current", True),
    ("running", "other", False),
    ("stopped", "current", False),
    ("not installed", None, False),
])
def test_running_service_requires_running_and_matching_directory(monkeypatch, tmp_path, status, target, expected):
    from types import SimpleNamespace
    from kronbot import cli
    current = tmp_path / "state"
    path = current if target == "current" else tmp_path / "other" if target else None
    monkeypatch.setattr(cli, "_service_manager", lambda: SimpleNamespace(
        status=lambda: status, configured_state_dir=lambda: path))
    assert cli._service_running(current) is expected


@pytest.mark.parametrize(("initial", "target", "paused", "blocked", "after", "code", "starts"), [
    ("not installed", "current", False, False, "running", 1, 0),
    ("stopped", "other", False, False, "running", 1, 0),
    ("running", "current", False, False, "running", 0, 0),
    ("stopped", "current", True, False, "running", 3, 0),
    ("stopped", "current", False, True, "running", 3, 0),
    ("stopped", "current", False, False, "running", 0, 1),
    ("stopped", "current", False, False, "stopped", 1, 1),
])
def test_doctor_service_safety(monkeypatch, tmp_path, capsys, initial, target, paused, blocked, after, code, starts):
    from types import SimpleNamespace
    from kronbot import cli

    directory = tmp_path / "state"
    with StateStore(directory) as store:
        store.data["background_paused"] = paused
        if blocked:
            store.data["blocked_until"]["my.itmo.ru"] = int(time.time()) + 3600
        store.save()
    calls = []
    manager = SimpleNamespace(
        status=lambda: after if calls else initial,
        configured_state_dir=lambda: directory if target == "current" else tmp_path / "other",
        start=lambda: calls.append("start"),
    )
    monkeypatch.setattr(cli, "_service_manager", lambda: manager)
    monkeypatch.setattr(cli, "state_directory", lambda: directory)
    clock = iter((0, 3))
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock, 3))
    assert cli._doctor() == code
    assert len(calls) == starts
    output = capsys.readouterr()
    if initial == "not installed":
        assert "kronbot service install" in output.out
    if starts and after == "stopped":
        assert "не запустилась" in output.err
        assert "служба запущена" not in output.out.lower()
    with StateStore(directory) as store:
        assert store.data["background_paused"] == paused
        assert bool(store.data["blocked_until"]) == blocked


def test_doctor_locked_state_never_starts(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace
    from kronbot import cli

    directory = tmp_path / "state"
    calls = []
    manager = SimpleNamespace(status=lambda: "stopped", configured_state_dir=lambda: directory,
                              start=lambda: calls.append(True))
    monkeypatch.setattr(cli, "_service_manager", lambda: manager)
    monkeypatch.setattr(cli, "state_directory", lambda: directory)
    with StateStore(directory):
        assert cli._doctor() == 1
    assert not calls
    assert "занято" in capsys.readouterr().err


def test_doctor_start_failure_never_reports_success(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace
    from kronbot import cli

    directory = tmp_path / "state"
    def fail():
        raise RuntimeError("secret")
    manager = SimpleNamespace(status=lambda: "stopped", configured_state_dir=lambda: directory, start=fail)
    monkeypatch.setattr(cli, "_service_manager", lambda: manager)
    monkeypatch.setattr(cli, "state_directory", lambda: directory)
    assert cli._doctor() == 1
    output = capsys.readouterr()
    assert "secret" not in output.err
    assert "служба запущена" not in output.out.lower()
    assert "kronbot service install" in output.err


@pytest.mark.parametrize(("argv", "tty", "expected"), [
    (["doctor"], True, True),
    (["doctor"], False, False),
    (["browser", "install"], True, False),
    (["service", "run", "--state-dir", "/tmp/state"], True, False),
    (["book", "1"], True, False),
    (["watch"], True, False),
    (["--help"], True, False),
])
def test_update_check_only_interactive_cli(monkeypatch, capsys, argv, tty, expected):
    from types import SimpleNamespace
    from kronbot import cli

    calls = []
    monkeypatch.setattr(sys, "argv", ["kronbot", *argv])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: tty)
    monkeypatch.setattr(cli, "check_update", lambda: calls.append(True) or "Доступно обновление")
    monkeypatch.setattr(cli, "_doctor", lambda: 0)
    monkeypatch.setattr(cli, "_service_command", lambda args: 0)
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(cli, "_service_notice", lambda *args, **kwargs: "")
    class Store:
        data = {"background_paused": False}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(cli, "StateStore", Store)
    async def run(*args):
        return 0
    monkeypatch.setattr(cli, "_run", run)
    if argv == ["--help"]:
        with pytest.raises(SystemExit):
            cli.main()
    else:
        assert cli.main() == 0
    assert bool(calls) is expected
    assert ("Доступно обновление" in capsys.readouterr().err) is expected


def test_update_checker_failure_is_silent(monkeypatch, capsys):
    from kronbot import cli
    def fail():
        raise OSError("secret")
    monkeypatch.setattr(cli, "check_update", fail)
    cli._interactive_update_notice()
    assert not capsys.readouterr().err
