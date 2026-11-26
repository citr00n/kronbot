import os
import asyncio
import subprocess
import sys
from datetime import datetime, timezone

import httpx
import pytest

from kronbot.cli import execute, parser
from kronbot.sport import LocalLimit, SportClient
from kronbot.state import StateStore


def invoke(*args, input=None, env=None):
    return subprocess.run([sys.executable, "-m", "kronbot", *args], input=input, text=True, capture_output=True, env=env)


def test_help_and_risk_gate_without_tty(tmp_path):
    env = {**os.environ, "KRONBOT_STATE_DIR": str(tmp_path / "not-created")}
    help_result = invoke("--help", env=env)
    assert help_result.returncode == 0 and "schedule" in help_result.stdout
    denied = invoke("book", "500001", env=env)
    assert denied.returncode == 2 and "неофициально" in denied.stderr
    assert not (tmp_path / "not-created").exists()
    password = invoke("--accept-risk", "login", "password", input="user\nsecret\n", env=env)
    assert password.returncode == 2 and "терминал" in password.stderr
    assert "secret" not in password.stdout + password.stderr


async def test_queue_and_single_booking(tmp_path, capsys):
    now = 1900000000
    stamp = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append(request.content)
            return httpx.Response(200, json={"error_code": 0, "result": [500001]})
        if request.url.path.endswith("/sign/schedule"):
            lessons = [{"id": ident, "date": stamp, "section_name": "Секция", "room_name": "Зал", "section_level": 1, "type_id": 2, "available": 1, "signed": False, "can_sign_in": {"can_sign_in": True}} for ident in (500001, 500002)]
            return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": lessons}]})
        if request.url.path.endswith("/personal/calendar"):
            return httpx.Response(200, json={"error_code": 0, "result": []})
        return httpx.Response(200, json={"error_code": 0, "result": {"free_attempts": 1, "can_sign_in": True}})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "owner", "access_token": "access", "access_expires_at": now + 10000, "refresh_token": None, "refresh_expires_at": None}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handler), now=lambda: now)
        async def run(*argv):
            return await execute(parser().parse_args(["--accept-risk", *argv]), store, client, now=lambda: now)
        assert await run("schedule") == 0
        assert "500001" in capsys.readouterr().out
        assert await run("queue", "add", "500002") == 0
        assert not posts
        assert await run("book", "500001") == 0
        assert posts == [b"[500001]"]
        with pytest.raises(LocalLimit):
            await run("book", "500002")
        assert posts == [b"[500001]"]
        assert await run("queue", "list") == 0
        assert "500002" in capsys.readouterr().out
        assert await run("queue", "remove", "500001") == 0
        assert len(posts) == 1
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
        task = asyncio.create_task(execute(parser().parse_args(["--accept-risk", "watch"]), store, client, cancel=cancel, now=lambda: now))
        await reached.wait()
        cancel.set()
        client.stopped = True
        release.set()
        assert await task == 0
        item = store.data["queues"]["owner"][0]
        assert item["status"] == ("booked" if stop_at_post else "waiting")
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
        result = await execute(parser().parse_args(["--accept-risk", "book", "1"]), store, client, now=lambda: now)
        assert result == 4
        output = capsys.readouterr().out
        assert "Проверьте в My ITMO" in output and "private response" not in output
        assert store.data["queues"]["owner"][0]["status"] == "uncertain"
        await client.close()
