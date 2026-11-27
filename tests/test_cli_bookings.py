from datetime import datetime, timezone

import httpx
import pytest

from kronbot import cli
from kronbot.agent import QueueError
from kronbot.auth import Session
from kronbot.sport import SportClient
from kronbot.state import StateStore


async def test_cli_shows_verified_isu_after_persisting_session(tmp_path, monkeypatch, capsys):
    class FakeAuth:
        def __init__(self, **kwargs):
            pass
        async def import_access(self, token):
            return Session("account-id\n123", "synthetic", 1900001000, None, None)
    monkeypatch.setattr(cli, "ItmoAuth", FakeAuth)
    monkeypatch.setattr(cli, "_text_login", lambda method: ("", "secret"))
    with StateStore(tmp_path / "state") as store:
        def profile(request):
            assert store.data["session"]["access_token"] == "synthetic"
            assert request.url.path == "/api/eduPlanNew/programs"
            assert request.headers["authorization"] == "Bearer synthetic"
            return httpx.Response(200, json={"error_code": 0, "result": {"isu": 123456, "programs": []}})
        client = SportClient(store, now=lambda: 1900000000, transport=httpx.MockTransport(profile))
        code = await cli.execute(cli.parser().parse_args(["login", "access-token"]), store, client, now=lambda: 1900000000)
        await client.close()
    output = capsys.readouterr().out
    assert code == 0 and "ИСУ: 123456" in output
    assert "account-id" not in output and "secret" not in output and "synthetic" not in output


async def test_cli_lists_and_cancels_booking(tmp_path, capsys):
    now = 1900000000
    stamp = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
    deletes = []
    def handle(request):
        if request.method == "DELETE":
            deletes.append(request.content)
            return httpx.Response(200, json={"error_code": 0, "result": [123]})
        return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [{"id": 123, "date": stamp, "section_name": "Секция", "room_name": "Зал", "section_level": 1, "signed": False}]}]})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "account", "access_token": "access", "access_expires_at": now + 5000, "refresh_token": None, "refresh_expires_at": None}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handle), now=lambda: now)
        assert await cli.execute(cli.parser().parse_args(["bookings"]), store, client, now=lambda: now) == 0
        assert "123" in capsys.readouterr().out
        assert await cli.execute(cli.parser().parse_args(["cancel", "123", "--yes"]), store, client, now=lambda: now) == 0
        assert deletes == [b"[123]"]
        assert "отменена" in capsys.readouterr().out
        await client.close()


async def test_cli_group_requires_explicit_whole_group(tmp_path):
    now = 1900000000
    stamp = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
    deletes = []
    def handle(request):
        if request.method == "DELETE":
            deletes.append(request)
        return httpx.Response(200, json={"error_code": 0, "result": [{"lessons": [{"id": 123, "date": stamp, "section_name": "Секция", "room_name": "Зал", "section_level": 2, "lesson_group_id": 45}]}]})
    with StateStore(tmp_path / "state") as store:
        store.data["session"] = {"sub": "account", "access_token": "access", "access_expires_at": now + 5000, "refresh_token": None, "refresh_expires_at": None}
        store.save()
        client = SportClient(store, transport=httpx.MockTransport(handle), now=lambda: now)
        with pytest.raises(QueueError, match="всей группы"):
            await cli.execute(cli.parser().parse_args(["cancel", "123", "--yes"]), store, client, now=lambda: now)
        assert not deletes
        await client.close()
